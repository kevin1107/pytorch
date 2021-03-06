from . import (
    RpcBackendOptions,
    WorkerInfo,
    _cleanup_python_rpc_handler,
    _destroy_rref_context,
    _invoke_remote_builtin,
    _invoke_remote_python_udf,
    _invoke_rpc_builtin,
    _invoke_rpc_python_udf,
    _set_rpc_timeout,
    _start_rpc_agent,
    backend_registry,
)
from .internal import (
    PythonUDF,
    RPCExecMode,
    _internal_rpc_pickler,
    _start_record_function,
)

import contextlib
from datetime import timedelta
import functools
import numbers
import sys
import logging
import threading
import torch
import torch.distributed as dist

logging.basicConfig()
logger = logging.getLogger(__name__)

_agent = None
# NB: Ignoring RRef leaks during shutdown. Without this, applications have to
# make sure there is no references to any RRef in the application code and
# Python GC has done its job to delete those RRefs. This is could result in bad
# debugging experiences especially when for large applications. Therefore, by
# default, we are going to ignore RRef leaks during shutdown. This is usually
# fine as shutdown means applications have done training and no longer care
# about states.
#
# To enable RRef leak checking, set this _ignore_rref_leak to False
_ignore_rref_leak = True
_default_pickler = _internal_rpc_pickler

@contextlib.contextmanager
def _use_rpc_pickler(rpc_pickler):
    r"""
    rpc_pickler: (.internal._InternalRPCPickler) Overrides the default RPC pickler
    """
    global _default_pickler
    _default_pickler = rpc_pickler
    try:
        yield
    finally:
        _default_pickler = _internal_rpc_pickler


def _require_initialized(func):
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        if _agent is None:
            raise RuntimeError(
                "RPC has not been initialized. Call "
                "torch.distributed.rpc.init_rpc first."
            )
        return func(*args, **kwargs)
    return wrapper


# States used by `def _wait_all_workers()`.
# `_ALL_WORKER_NAMES` is initialized on initiaizing RPC layer.
_ALL_WORKER_NAMES = None
# `_SHUTDOWN_INTENT_WORKER_NAMES` is an empty set at beginning.
# It's only used by leader worker. Leader worker is elected as the first
# worker in a sorted worker name list.
# Whenever there is a worker showing shutdown intention to the leader, by
# calling _wait_all_workers()`, the leader adds this worker's name to the set.
# The leader also adds itself's name to the set on calling
# `_wait_all_workers()`. We need this because, we confine `_wait_all_workers()`
# to be called only once, by examing if leader's name has been added to the set.
_SHUTDOWN_INTENT_WORKER_NAMES = set()
# Once `_SHUTDOWN_INTENT_WORKER_NAMES == _ALL_WORKER_NAMES`,
# we flip `_SHUTDOWN_PROCEED_SIGNAL` on the leader, and leader will send RPCs
# to follower workers to flip their `_SHUTDOWN_PROCEED_SIGNAL`s.
_SHUTDOWN_PROCEED_SIGNAL = threading.Event()


def _on_leader_follower_report_shutdown_intent(worker_name):
    assert (
        worker_name in _ALL_WORKER_NAMES
    ), "{worker_name} is not expected by leader.".format(worker_name=worker_name)
    assert (
        worker_name not in _SHUTDOWN_INTENT_WORKER_NAMES
    ), "{worker_name} reported intent twice. ".format(worker_name=worker_name)
    _SHUTDOWN_INTENT_WORKER_NAMES.add(worker_name)
    if _ALL_WORKER_NAMES == _SHUTDOWN_INTENT_WORKER_NAMES:
        _set_proceed_shutdown_signal()


def _set_proceed_shutdown_signal():
    assert not _SHUTDOWN_PROCEED_SIGNAL.is_set(), "Termination signal got set twice."
    _SHUTDOWN_PROCEED_SIGNAL.set()


@_require_initialized
def _wait_all_workers():
    r"""
    Block until all local and remote RPC processes reach this method and wait
    for all outstanding work to complete. Every RPC process must call this
    method before exit to perform a graceful shutdown. This should be used to
    terminate the RPC framework, and there is no guarantee that the RPC
    framework will work after this method returns.
    """
    assert (
        _ALL_WORKER_NAMES is not None
    ), "`_ALL_WORKER_NAMES` is not initialized for `def _wait_all_workers`."
    leader_worker_name = sorted(_ALL_WORKER_NAMES)[0]

    self_worker_name = _agent.get_worker_info().name
    assert (
        self_worker_name not in _SHUTDOWN_INTENT_WORKER_NAMES
    ), "Can not call `_wait_all_workers()` twice."

    is_leader_worker = leader_worker_name == self_worker_name

    # Phase 1: Followers send intents.
    # All followers report intents to the leader.
    if is_leader_worker:
        _on_leader_follower_report_shutdown_intent(self_worker_name)
    else:
        rpc_sync(
            leader_worker_name,
            _on_leader_follower_report_shutdown_intent,
            args=(self_worker_name,),
        )

    _SHUTDOWN_PROCEED_SIGNAL.wait()

    # Phase 2: Leader asks followers to proceed.
    # Leader's signal is the first to be unblocked,
    # after receiving all followers' intents.
    if is_leader_worker:
        # The leader sends out proceeed signals to all followers.
        timeout = timedelta(seconds=5)
        _set_rpc_timeout(timeout)
        worker_name_to_response_future_dict = dict()
        for follower_worker_name in _ALL_WORKER_NAMES - {leader_worker_name}:
            fut = rpc_async(follower_worker_name, _set_proceed_shutdown_signal, args=())
            worker_name_to_response_future_dict[follower_worker_name] = fut
        for follower_worker_name, fut in worker_name_to_response_future_dict.items():
            try:
                fut.wait()
            except RuntimeError as ex:
                logger.error(
                    "{worker_name} failed to respond to 'Shutdown Proceed.' request in {timeout}".format(
                        worker_name=follower_worker_name,
                        timeout=timeout,
                    )
                )


@_require_initialized
def shutdown(graceful=True):
    r"""
    Perform a shutdown of the RPC agent, and then destroy the RPC agent. This
    stops the local agent from  accepting outstanding requests, and shuts
    down the RPC framework by terminating all RPC threads. If graceful=True,
    then this will block until all local and remote RPC processes reach this
    method and wait for all outstanding work to complete. Otherwise, if
    graceful=False, then this is a local shutdown, and it does not wait for
    other RPC processes to reach this method.

    Arguments:
        graceful (bool): Whether to do a graceful shutdown or not. If True,
                         this will block until all local and remote RPC
                         processes have reached this method and wait for all
                         outstanding work to complete.

    Example::
        Make sure that ``MASTER_ADDRESS`` and ``MASTER_PORT`` are set properly
        on both workers. Refer to :meth:`~torch.distributed.init_process_group`
        API for more details. For example,

        >>> export MASTER_ADDRESS=localhost
        >>> export MASTER_port=5678

        Then run the following code in two different processes:

        >>> # On worker 0:
        >>> import torch
        >>> import torch.distributed.rpc as rpc
        >>> rpc.init_rpc("worker0", rank=0, world_size=2)
        >>> # do some work
        >>> result = rpc.rpc_sync("worker1", torch.add, args=(torch.ones(1), 1))
        >>> # ready to shutdown
        >>> rpc.shutdown()

        >>> # On worker 1:
        >>> import torch.distributed.rpc as rpc
        >>> rpc.init_rpc("worker1", rank=1, world_size=2)
        >>> # wait for worker 0 to finish work, and then shutdown.
        >>> rpc.shutdown()
    """
    global _agent

    if graceful:
        _wait_all_workers()
        _agent.join()
    try:
        # This raises a `TORCH_CHECK()` exception on RRef leak detected.
        _destroy_rref_context(_ignore_rref_leak)
    finally:
        _agent.shutdown()
        # clean up python rpc handler in shutdown(), see comments in
        # PythonRpcHandler::cleanup(), call it in python API because the
        # cleanup() function has python dependency, it assumes python
        # interpreter exists.
        # No matter if RRef leak exception is raised, this clean-up code
        # must run to avoid destruction segfault in Python 3.5.
        _cleanup_python_rpc_handler()
        _agent = None


# TODO: add a context manager to wrap _init_rpc_backend and shutdown
def _init_rpc_backend(
    backend=backend_registry.BackendType.PROCESS_GROUP,
    store=None,
    name=None,
    rank=-1,
    world_size=-1,
    rpc_backend_options=None,
):

    if sys.version_info < (3, 0):
        raise RuntimeError("RPC package does not support Python2.")

    _validate_rpc_args(backend, store, name, rank, world_size, rpc_backend_options)

    global _agent

    if _agent:
        raise RuntimeError("RPC is already initialized")

    # Initialize RPC.
    _agent = backend_registry.init_backend(
        backend,
        store=store,
        name=name,
        rank=rank,
        world_size=world_size,
        rpc_backend_options=rpc_backend_options,
    )

    worker_infos = _agent.get_worker_infos()
    global _ALL_WORKER_NAMES
    _ALL_WORKER_NAMES = {worker_info.name for worker_info in worker_infos}

    _start_rpc_agent(_agent)


@_require_initialized
def get_worker_info(worker_name=None):
    r"""
    Get :class:`~torch.distributed.rpc.WorkerInfo` of a given worker name.
    Use this :class:`~torch.distributed.rpc.WorkerInfo` to avoid passing an
    expensive string on every invocation.

    Arguments:
        worker_name (str): the string name of a worker. If ``None``, return the
                           the id of the current worker. (default ``None``)

    Returns:
        :class:`~torch.distributed.rpc.WorkerInfo` instance for the given
        ``worker_name`` or :class:`~torch.distributed.rpc.WorkerInfo` of the
        current worker if ``worker_name`` is ``None``.
    """
    if worker_name:
        return _agent.get_worker_info(worker_name)
    else:
        return _agent.get_worker_info()


def _to_worker_info(name_or_info):
    if isinstance(name_or_info, WorkerInfo):
        return name_or_info
    elif isinstance(name_or_info, str):
        return get_worker_info(name_or_info)
    else:
        raise ValueError("Cannot get WorkerInfo from name {}".format(name_or_info))

def _validate_rpc_args(backend, store, name, rank, world_size, rpc_backend_options):
    type_mapping = {
        backend: backend_registry.BackendType,
        store: dist.Store,
        name: str,
        rank: numbers.Integral,
        world_size: numbers.Integral,
        rpc_backend_options: RpcBackendOptions,
    }
    for arg, arg_type in type_mapping.items():
        if not isinstance(arg, arg_type):
            raise RuntimeError(
                "Argument {} must be of type {} but got type {}".format(
                    arg, arg_type, type(arg)
                )
            )


@_require_initialized
def remote(to, func, args=None, kwargs=None):
    r"""
    Make a remote call to run ``func`` on worker ``to`` and return an
    :class:`~torch.distributed.rpc.RRef` to the result value immediately.
    Worker ``to`` will be the owner of the returned
    :class:`~torch.distributed.rpc.RRef`, and the worker calling ``remote`` is
    a user. The owner manages the global reference count of its
    :class:`~torch.distributed.rpc.RRef`, and the owner
    :class:`~torch.distributed.rpc.RRef` is only destructed when globally there
    are no living references to it.

    Arguments:
        to (str or WorkerInfo): id or name of the destination worker.
        func (callable): builtin functions (like :meth:`torch.add`).
        args (tuple): the argument tuple for the ``func`` invocation.
        kwargs (dict): is a dictionary of keyword arguments for the ``func``
                       invocation.

    Returns:
        A user :class:`~torch.distributed.rpc.RRef` instance to the result
        value. Use the blocking API :meth:`torch.distributed.rpc.RRef.to_here`
        to retrieve the result value locally.

    Example::
        Make sure that ``MASTER_ADDRESS`` and ``MASTER_PORT`` are set properly
        on both workers. Refer to :meth:`~torch.distributed.init_process_group`
        API for more details. For example,

        >>> export MASTER_ADDRESS=localhost
        >>> export MASTER_port=5678

        Then run the following code in two different processes:

        >>> # On worker 0:
        >>> import torch
        >>> import torch.distributed.rpc as rpc
        >>> rpc.init_rpc("worker0", rank=0, world_size=2)
        >>> rref1 = rpc.remote("worker1", torch.add, args=(torch.ones(2), 3))
        >>> rref2 = rpc.remote("worker1", torch.add, args=(torch.ones(2), 1))
        >>> x = rref1.to_here() + rref2.to_here()
        >>> rpc.shutdown()

        >>> # On worker 1:
        >>> import torch.distributed.rpc as rpc
        >>> rpc.init_rpc("worker1", rank=1, world_size=2)
        >>> rpc.shutdown()
    """
    qualified_name = torch.jit._find_builtin(func)
    info = _to_worker_info(to)

    # If profiling is enabled, kick off the timer and retrieve back a
    # RecordFunction instance.
    rf = None
    if torch.autograd._profiler_enabled():
        rf = _start_record_function(
            RPCExecMode.REMOTE,
            str(qualified_name) if qualified_name is not None else func.__qualname__,
            get_worker_info().name,
            info.name,
        )

    args = args if args else ()
    kwargs = kwargs if kwargs else {}

    if qualified_name is not None:
        return _invoke_remote_builtin(
            _agent, info, qualified_name, rf, *args, **kwargs)
    else:
        (pickled_python_udf, tensors) = _default_pickler.serialize(
            PythonUDF(func, args, kwargs))
        return _invoke_remote_python_udf(
            _agent, info, pickled_python_udf, tensors, rf)


def _invoke_rpc(to, func, rpc_type, args=None, kwargs=None):
    if not callable(func):
        raise TypeError("function should be callable.")

    qualified_name = torch.jit._find_builtin(func)
    info = _to_worker_info(to)
    # If profiling is enabled, kick off the timer and retrieve back a
    # RecordFunction instance.
    rf = None
    if torch.autograd._profiler_enabled():
        rf = _start_record_function(
            rpc_type,
            str(qualified_name) if qualified_name is not None else func.__qualname__,
            get_worker_info().name,
            info.name,
        )

    args = args if args else ()
    kwargs = kwargs if kwargs else {}

    if qualified_name is not None:
        fut = _invoke_rpc_builtin(
            _agent, info, qualified_name, rf, *args, **kwargs
        )
    else:
        (pickled_python_udf, tensors) = _default_pickler.serialize(
            PythonUDF(func, args, kwargs))
        fut = _invoke_rpc_python_udf(
            _agent, info, pickled_python_udf, tensors, rf)
    return fut


@_require_initialized
def rpc_sync(to, func, args=None, kwargs=None):
    r"""
    Make a blocking RPC call to run function ``func`` on worker ``to``. RPC
    messages are sent and received in parallel to execution of Python code. This
    method is thread-safe.

    Arguments:
        to (str or WorkerInfo): id or name of the destination worker.
        func (callable): any callable function. builtin functions (like
                         :meth:`torch.add`) can be sent over RPC more efficiently.
        args (tuple): the argument tuple for the ``func`` invocation.
        kwargs (dict): is a dictionary of keyword arguments for the ``func``
                       invocation.

    Returns:
        Returns the result of running ``func`` on ``args`` and ``kwargs``.

    Example::
        Make sure that ``MASTER_ADDRESS`` and ``MASTER_PORT`` are set properly
        on both workers. Refer to :meth:`~torch.distributed.init_process_group`
        API for more details. For example,

        >>> export MASTER_ADDRESS=localhost
        >>> export MASTER_port=5678

        Then run the following code in two different processes:

        >>> # On worker 0:
        >>> import torch
        >>> import torch.distributed.rpc as rpc
        >>> rpc.init_rpc("worker0", rank=0, world_size=2)
        >>> ret = rpc.rpc_sync("worker1", torch.add, args=(torch.ones(2), 3))
        >>> rpc.shutdown()

        >>> # On worker 1:
        >>> import torch.distributed.rpc as rpc
        >>> rpc.init_rpc("worker1", rank=1, world_size=2)
        >>> rpc.shutdown()
    """
    fut = _invoke_rpc(to, func, RPCExecMode.SYNC, args, kwargs)
    return fut.wait()


@_require_initialized
def rpc_async(to, func, args=None, kwargs=None):
    r"""
    Make a non-blocking RPC call to run function ``func`` on worker ``to``. RPC
    messages are sent and received in parallel to execution of Python code. This
    method is thread-safe. This method will immediately return a
    Future that can be awaited on.

    Arguments:
        to (str or WorkerInfo): id or name of the destination worker.
        func (callable): any callable function. builtin functions (like
                         :meth:`torch.add`) can be sent over RPC more efficiently.
        args (tuple): the argument tuple for the ``func`` invocation.
        kwargs (dict): is a dictionary of keyword arguments for the ``func``
                       invocation.

    Returns:
        Returns a Future object that can be waited
        on. When completed, the return value of ``func`` on ``args`` and
        ``kwargs`` can be retrieved from the Future object.

    Example::
        Make sure that ``MASTER_ADDRESS`` and ``MASTER_PORT`` are set properly
        on both workers. Refer to :meth:`~torch.distributed.init_process_group`
        API for more details. For example,

        >>> export MASTER_ADDRESS=localhost
        >>> export MASTER_port=5678

        Then run the following code in two different processes:

        >>> # On worker 0:
        >>> import torch
        >>> import torch.distributed.rpc as rpc
        >>> rpc.init_rpc("worker0", rank=0, world_size=2)
        >>> fut1 = rpc.rpc_async("worker1", torch.add, args=(torch.ones(2), 3))
        >>> fut2 = rpc.rpc_async("worker1", min, args=(1, 2))
        >>> result = fut1.wait() + fut2.wait()
        >>> rpc.shutdown()

        >>> # On worker 1:
        >>> import torch.distributed.rpc as rpc
        >>> rpc.init_rpc("worker1", rank=1, world_size=2)
        >>> rpc.shutdown()
    """
    fut = _invoke_rpc(to, func, RPCExecMode.ASYNC, args, kwargs)
    return fut
