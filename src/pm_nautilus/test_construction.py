"""Explicit TEST construction/close cleanup; LIVE uses its original path."""

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


def _attempt(owner, operation, *args):
    try:
        action = getattr(owner, operation, None)
        if callable(action):
            action(*args)
    except BaseException:
        # Disposal cannot replace the original exception or prevent independent
        # components/the database from being closed.
        pass


def release_test_runtime(runtime):
    """Break stopped TEST references without invoking clock-wide SDK disposal.

    Call after the ordinary financial callbacks and store close. Each Native
    owns its bus/cache/portfolio; no external clock callbacks are cleared here.
    This is also safe to repeat from shutdown/quarantine cleanup.
    """
    if getattr(runtime, "mode", None) != "TEST":
        raise ValueError("TEST资源释放器不接受LIVE")
    native = getattr(runtime, "native", None)
    bus = getattr(native, "bus", None)
    # Explicitly remove owner callbacks even if bus disposal itself fails.
    for topic, owner, name in (
        ("events.order.*", runtime, "on_native"),
        ("data.book.deltas.*", runtime, "_mark_test_preview_quote"),
        ("data.book.deltas.*", getattr(runtime, "strategy", None), "on_order_book_deltas"),
    ):
        handler = getattr(owner, name, None)
        if handler is not None:
            _attempt(bus, "unsubscribe", topic, handler)
    _attempt(bus, "dispose")
    _attempt(getattr(native, "portfolio", None), "dispose")
    _attempt(getattr(native, "cache", None), "reset")
    _attempt(getattr(native, "cache", None), "dispose")
    # Python owners can participate in Cython/Rust cycles invisible to Python
    # GC. Their callbacks have finished; detach this TEST account only.
    for resource in (
        getattr(runtime, "strategy", None),
        getattr(native, "strategy", None),
        getattr(runtime, "client", None),
        native,
    ):
        if resource is not None and getattr(resource, "owner", None) is runtime:
            try:
                resource.owner = None
            except BaseException:
                pass


def close_test_runtime(runtime):
    """Preserve pause -> Native.stop -> Store.close, then release TEST owners."""
    if getattr(runtime, "mode", None) != "TEST":
        raise ValueError("TEST关闭器不接受LIVE")
    if getattr(runtime, "_test_close_started", False):
        return
    runtime._test_close_started = True
    error = None
    for owner, operation in ((runtime, "pause"), (runtime.native, "stop")):
        try:
            getattr(owner, operation)()
        except BaseException as exc:
            if error is None:
                error = exc
    # A failing stop callback must not leave other account-local engines active.
    # Complete these stops while the journal remains open, as Native.stop does.
    for component in (
        getattr(runtime.native, "strategy", None),
        getattr(runtime.native, "execution", None),
        getattr(runtime.native, "risk", None),
        getattr(runtime.native, "data", None),
    ):
        if any(
            getattr(component, state, False)
            for state in ("is_running", "is_starting", "is_degraded")
        ):
            _attempt(component, "stop")
    try:
        runtime.store.close()
    except BaseException as exc:
        if error is None:
            error = exc
        _attempt(getattr(runtime.store, "db", None), "close")
    finally:
        release_test_runtime(runtime)
    if error is not None:
        raise error


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
