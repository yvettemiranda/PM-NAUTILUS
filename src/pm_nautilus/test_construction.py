"""Explicit disposal of failed TEST constructions; LIVE uses its original path."""

from .native import Native
from .strategy import Runtime


def _partial_native(runtime, error):
    native = getattr(runtime, "native", None)
    if native is not None:
        return native
    # A failed RHS has not assigned runtime.native yet. Its constructor or a
    # factory wrapper still owns it in the exception frames. Identity of the
    # owner, not a global registry or mutable constructor binding, defines scope.
    trace = error.__traceback__
    while trace is not None:
        for candidate in trace.tb_frame.f_locals.values():
            if isinstance(candidate, Native) and getattr(candidate, "owner", None) is runtime:
                return candidate
        trace = trace.tb_next
    return None


def _attempt(owner, operation):
    action = getattr(owner, operation, None)
    if callable(action):
        try:
            action()
        except BaseException:
            # Disposal cannot replace the original constructor exception or
            # prevent independent components/the database from being closed.
            pass


def _dispose_failed_test(runtime, error):
    native = _partial_native(runtime, error)
    if native is not None:
        components = []
        for component in (
            getattr(runtime, "strategy", None),
            getattr(native, "strategy", None),
            getattr(native, "execution", None),
            getattr(native, "risk", None),
            getattr(native, "data", None),
        ):
            if component is not None and all(component is not value for value in components):
                components.append(component)
        # Keep the journal open while original stop callbacks finish. Explicit
        # dispose also releases Cython MessageBus/engine reference cycles which
        # merely stopping a component doesn't release.
        for component in components:
            if any(
                getattr(component, state, False)
                for state in ("is_running", "is_starting", "is_degraded")
            ):
                _attempt(component, "stop")
        for component in components:
            _attempt(component, "dispose")
        _attempt(getattr(native, "bus", None), "dispose")
        _attempt(getattr(native, "cache", None), "dispose")
    store = getattr(runtime, "store", None)
    if store is not None:
        _attempt(store, "close")
    # Python subclasses of Cython strategy/client types can retain their owner
    # outside Python's cycle traversal even after dispose. These objects were
    # never published as a usable account; detach only this failed TEST owner.
    for resource in (
        getattr(runtime, "strategy", None),
        getattr(runtime, "client", None),
        native,
    ):
        if resource is not None and getattr(resource, "owner", None) is runtime:
            try:
                resource.owner = None
            except Exception:
                pass


def construct_test_runtime(
    runtime_type, path, mode="TEST", clock=None, live_factory=None, storage_guard=None
):
    """Retain ownership before __init__ so every failed TEST can be disposed."""
    if mode != "TEST":
        raise ValueError("TEST安全构造器不接受LIVE")
    if not isinstance(runtime_type, type) or not issubclass(runtime_type, Runtime):
        raise TypeError("TEST运行实例类型无效")
    runtime = runtime_type.__new__(runtime_type)
    try:
        runtime_type.__init__(runtime, path, mode, clock, live_factory, storage_guard)
    except BaseException as error:
        _dispose_failed_test(runtime, error)
        raise
    return runtime
