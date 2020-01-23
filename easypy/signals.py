from __future__ import absolute_import
import inspect
import threading
import functools
from itertools import chain, count
import logging
from enum import Enum
from contextlib import contextmanager, ExitStack
import weakref

from easypy.concurrency import Futures, MultiObject
from easypy.decorations import parametrizeable_decorator
from easypy.exceptions import TException
from easypy.contexts import is_contextmanager
from easypy.misc import kwargs_resilient, WeakMethodDead
from easypy.collections import separate, collect_with

PRIORITIES = Enum("PRIORITIES", "FIRST NONE LAST")
_logger = logging.getLogger(__name__)


ids = set()


class MissingIdentifier(TException):
    template = "signal handler {_signal_name} must be called with a '{_identifier}' argument"


def make_id(name):
    # from md5 import md5
    # id = md5(name).hexdigest()[:5].upper()
    for c in range(10):
        id = "".join(word[0] for word in name.split("_")) + (str(c) if c else "")
        if id not in ids:
            break
    ids.add(id)
    return id


def get_original_func(func):
    while True:
        if isinstance(func, functools.partial):
            func = func.func
        elif hasattr(func, "__wrapped__") and not (
            is_contextmanager(func) and not is_contextmanager(func.__wrapped__)
            # This means func is the context manager itself (which wraps a non-context-manager function)
        ):
            func = func.__wrapped__
        else:
            return func


class RemoveHandler(Exception):
    pass


STALE_HANDLER = (WeakMethodDead, RemoveHandler)


class SignalHandler(object):

    _idx_gen = count(100)

    def __init__(self, func, asynchronous=False, priority=PRIORITIES.NONE, times=None, identifier=None, **kw):
        self.func = kwargs_resilient(func)
        self._func = get_original_func(func)  # save the original funtion so we can unregister based on the function

        # identifier only applicable to methods
        if inspect.ismethod(self._func) or isinstance(self._func, weakref.WeakMethod):
            self.identifier = identifier
        else:
            self.identifier = None

        # Backwards compatibility
        self.asynchronous = kw.pop("async", asynchronous)
        assert not kw, "SignalHandler's kwargs should contain only 'async' argument for backwards compatibility"

        self.priority = priority
        self.times = times
        self.idx = next(self._idx_gen)

        if isinstance(func, weakref.WeakMethod):
            func = func()  # to allow accessing it's __code__ and __name__
        self.filename = func.__code__.co_filename
        self.lineno = func.__code__.co_firstlineno
        self.name = self.__name__ = func.__name__

    def __repr__(self):
        return "<handler #{0.idx} '{0.name}' ({0.filename}:{0.lineno})>".format(self)

    def should_run(self, **kwargs):
        if self.times == 0:
            return False

        if self.identifier:
            try:
                handler_object = self._func.__self__
            except AttributeError:
                func = self._func()
                if func is None:
                    raise WeakMethodDead
                handler_object = func.__self__
            target_object = kwargs[self.identifier]

            if hasattr(self._func, 'identifier_path'):
                # special case if the signal method defined a user-defined attribute path for fiding the associated identifier
                path = self._func.identifier_path
                handler_object = eval('obj.%s' % path.strip('.'), dict(obj=handler_object), {})
            elif hasattr(handler_object, self.identifier):
                # the identifier exists as an attribute on the handler object, named the same
                handler_object = getattr(handler_object, self.identifier)
            elif isinstance(handler_object, target_object.__class__):
                # the handler_object is itself of the same type as the target_object, so we should regard it is the identifier itself
                pass
            else:
                # the handler_object isn't identifiable, so we'll just pass it the identifier and hope it knows what to do
                handler_object = None

            if handler_object is not None and handler_object != target_object:
                return False
        return True

    def __call__(self, *, swallow_exceptions, **kwargs):
        if self.times is not None:
            self.times -= 1

        try:
            return self.func(**kwargs)
        except STALE_HANDLER:
            self.times = 0
            raise
        except:
            if not swallow_exceptions:
                raise
            _logger.silent_exception("Exception in (%s) ignored", self)

    @property
    def bound_object(self):
        if isinstance(self._func, weakref.WeakMethod):
            return weakref.ref.__call__(self._func)
        else:
            return None


class Signal:

    ALL = {}

    def __new__(cls, name, asynchronous=None, swallow_exceptions=False, log=True, signature=None):
        if signature is None:
            # Two signals without a signature that have the same name are identical
            try:
                return cls.ALL[name]
            except KeyError:
                pass
        assert threading.main_thread() is threading.current_thread(), "Can only create Signal objects from the MainThread"
        signal = object.__new__(cls)
        # we use this to track the calling of various callbacks. we use this short id so it doesn't overflow the logs
        signal.id = make_id(name)
        signal.handlers = {priority: [] for priority in PRIORITIES}
        signal.name = name
        signal.swallow_exceptions = swallow_exceptions
        signal.asynchronous = asynchronous
        signal.identifier = None
        signal.log = log
        signal.signature = signature
        if signature is None:
            return cls.ALL.setdefault(name, signal)
        else:
            return signal

    def iter_handlers(self):
        return chain(*(self.handlers[k] for k in PRIORITIES))

    def iter_handlers_by_priority(self):
        for k in PRIORITIES:
            yield separate(self.handlers[k], lambda h: h.asynchronous)

    def remove_handler(self, handler):
        self.handlers[handler.priority].remove(handler)
        _logger.debug("handler removed from '%s' (%s): %s", self.name, handler.priority.name, handler)

    def remove_handlers_if_exist(self, handlers):
        if not handlers:
            return
        handlers = set(handlers)
        for priority in {h.priority for h in handlers}:
            self.handlers[priority][:] = (handler for handler in self.handlers[priority] if handler not in handlers)

    def handler(self, func=None, asynchronous=None, priority=PRIORITIES.NONE, **kw):
        def wrapper(func):
            handler_signature = inspect.signature(func)
            try:
                bound_arg, *free_args = handler_signature.parameters.values()
            except ValueError:
                raise TypeError('%s cannot be signal handler - it has no arguments and cannot be a method' % (func,))
            if bound_arg.name in self.signature.parameters:
                raise TypeError(
                    '%s cannot be signal handler - first argument is %s, a parameter of %s and not the `self` argument' % (
                        func, bound_arg, self))
            self._verify_handler_func(func, handler_signature.replace(parameters=free_args))
            signal_handler_for = func.__dict__.setdefault('_signal_handler_for', weakref.WeakKeyDictionary())
            signal_handler_for[self] = dict(
                asynchronous=asynchronous,
                priority=priority,
                **kw)
            return func

        if func is None:
            return wrapper
        else:
            return wrapper(func)

    def register(self, func=None, asynchronous=None, priority=PRIORITIES.NONE, times=None, **kw):
        # backwards compatibility
        asynchronous = kw.pop('async', asynchronous)
        assert not kw

        if not func:
            return functools.partial(self.register, asynchronous=asynchronous, priority=priority)

        self._verify_handler_func(func)

        if asynchronous is None:
            asynchronous = False if self.asynchronous is None else self.asynchronous
        elif self.asynchronous is not None:
            assert self.asynchronous == asynchronous, "Signal is set with asynchronous=%s" % self.asynchronous
        handler = SignalHandler(func, asynchronous, priority, times=times, identifier=self.identifier)
        self.handlers[priority].append(handler)
        _logger.debug("registered handler for '%s' (%s): %s", self.name, priority.name, handler)
        return func

    def unregister(self, func):
        for handler in self.iter_handlers():
            if func in (
                    handler._func,  # simple case
                    getattr(handler._func, "__wrapped__", None),  # wrapped with @wraps
                    getattr(handler._func, "func", None),  # wrapped with 'partial'
                    handler._func() if isinstance(handler._func, weakref.WeakMethod) else None,
                    ):
                self.remove_handler(handler)

    @contextmanager
    def registered(self, func, **kwargs):
        self.register(func, **kwargs)
        try:
            yield
        finally:
            self.unregister(func)

    def _verify_handler_func(self, handler_func, handler_signature=None):
        if handler_signature is None:
            handler_signature = inspect.signature(handler_func)
        bad_parameters = ', '.join(
            '%s is %s' % (p, p.kind)
            for p in handler_signature.parameters.values()
            if p.kind in (p.POSITIONAL_ONLY, p.VAR_POSITIONAL))
        if bad_parameters:
            raise TypeError('%s has illegal parameters for signal handler: %s' % (handler_func, bad_parameters))
        if self.signature is None:
            return
        extra_parameters = ', '.join(
            parameter.name
            for parameter in handler_signature.parameters.values()
            if parameter.kind is not parameter.VAR_KEYWORD
            and parameter.default is parameter.empty
            and parameter.name not in self.signature.parameters)
        if extra_parameters:
            raise TypeError('%s has parameters not in signal: %s' % (handler_func, extra_parameters))

    def _verify_call_args(self, kwargs):
        if self.signature is None:
            return
        binding = self.signature.bind(**kwargs)
        for parameter in self.signature.parameters.values():
            if parameter.annotation is parameter.empty:
                continue
            if isinstance(parameter.annotation, type):
                value = binding.arguments[parameter.name]
                if not isinstance(value, parameter.annotation):
                    raise TypeError('Expected {parameter.name}: {parameter.annotation}, got {value_type}'.format(
                        parameter=parameter,
                        value_type=type(value)))

    def __call__(self, **kwargs):
        self._verify_call_args(kwargs)
        if not self.identifier:
            pass
        elif self.identifier in kwargs:
            pass
        else:
            raise MissingIdentifier(_signal_name=self.name, _identifier=self.identifier)

        if self.log:
            _logger.debug("Triggered %s", self)

        for handler in self.iter_handlers():
            if handler.times == 0:
                self.remove_handler(handler)

        kwargs.setdefault('swallow_exceptions', self.swallow_exceptions)

        with ExitStack() as STACK:
            STACK.enter_context(_logger.context(self.id))

            if any(h.asynchronous for h in self.iter_handlers()):
                futures = STACK.enter_context(Futures.executor())
            else:
                futures = None

            handlers_to_remove = []

            def run_handler(handler):
                try:
                    with _logger.context("#%03d" % handler.idx):
                        if not handler.should_run(**kwargs):
                            return
                        handler(**kwargs)
                except STALE_HANDLER:
                    handlers_to_remove.append(handler)

            for async_handlers, synced_handlers in self.iter_handlers_by_priority():

                for handler in async_handlers:
                    futures.submit(run_handler, handler)

                for handler in synced_handlers:
                    run_handler(handler)

                if futures:
                    for future in futures.as_completed():
                        future.result()        # bubble up exceptions

        self.remove_handlers_if_exist(handlers_to_remove)

    def __str__(self):
        return "<Signal %s (%s)>" % (self.name, self.id)

    __repr__ = __str__


@parametrizeable_decorator
def signal_identifier_path(func, path):
    func.identifier_path = path
    return func


class ContextManagerSignal(Signal):
    @contextmanager
    def __call__(self, **kwargs):
        if self.log:
            # log signal for centralized logging analytics.
            # minimize text message as most of the data is sent in the 'extra' dict
            # signal fields get prefixed with 'sig_', and the values are repr'ed
            signal_fields = {("sig_%s" % k): repr(v) for (k, v) in kwargs.items()}
            _logger.debug("Triggered '%s' (%s) - entering", self.name, self.id,
                          extra=dict(signal_fields, signal=self.name, signal_id=self.id))

        for handler in self.iter_handlers():
            if handler.times == 0:
                self.unregister(handler.func)

        kwargs.setdefault('swallow_exceptions', self.swallow_exceptions)

        with ExitStack() as handlers_stack:
            handlers_to_remove = []

            def should_run(handler):
                try:
                    return handler.should_run(**kwargs)
                except STALE_HANDLER:
                    pass
                handlers_to_remove.append(handler)
                return False

            indexer = count()

            @contextmanager
            def run_handler(handler):
                index = next(indexer)
                already_yielded = False
                try:
                    with _logger.context(self.id), _logger.context("%02d" % index):
                        with handler(**kwargs):
                            yield
                            already_yielded = True
                except STALE_HANDLER:
                    handlers_to_remove.append(handler)
                    if not already_yielded:
                        yield

            for async_handlers, synced_handlers in self.iter_handlers_by_priority():
                async_handlers = MultiObject(filter(should_run, async_handlers))
                synced_handlers = list(filter(should_run, synced_handlers))

                if async_handlers:
                    handlers_stack.enter_context(MultiObject(async_handlers).call(run_handler))

                for handler in synced_handlers:
                    handlers_stack.enter_context(run_handler(handler))
            yield

        self.remove_handlers_if_exist(handlers_to_remove)


@parametrizeable_decorator
def signal(func, asynchronous=None, swallow_exceptions=False, log=True):
    """
    Define a signal

    :param func: The (decorated) function that defines the signal name and signature.
    :param bool asynchronous: Set to enforce synchronous/asynchronous for all handlers.
    :param bool swallow_exceptions: Ignore exceptions in handlers (ensures all handlers will run)
    :param bool log: Log signal registration and invocation.

    Example::

        @signal
        def my_signal(a, b: int): ...

    Handlers registered on the signal will fail if they arguments no in the
    signal. Calls to the signal will fail if they have arguments not declared
    in the signal, or arguments with wrong types if the signal annotates the
    argument.
    """
    if is_contextmanager(func):
        cls = ContextManagerSignal
    else:
        cls = Signal

    return cls(
        func.__name__,
        asynchronous=asynchronous,
        swallow_exceptions=swallow_exceptions,
        log=log,
        signature=inspect.signature(func))


####################################
def register_signal(name, func, **kwargs):
    signal = Signal(name)
    signal.register(func, **kwargs)
    return functools.partial(signal.unregister, func)


def unregister_signal(name, func):
    try:
        signal = Signal.ALL[name]
    except KeyError:
        return
    return signal.unregister(func)


@parametrizeable_decorator
def register(func, **kwargs):
    register_signal(func.__name__, func, **kwargs)
    return func


def unregister(func):
    return unregister_signal(func.__name__, func)


def _set_handler_params(**kw):
    def inner(func):
        params = getattr(func, '_signal_handler_params', {})
        params.update(kw)
        func._signal_handler_params = params
        return func
    return inner


run_first = _set_handler_params(priority=PRIORITIES.FIRST)
run_last = _set_handler_params(priority=PRIORITIES.LAST)
run_async = _set_handler_params(asynchronous=True)
run_sync = _set_handler_params(asynchronous=False)


@functools.lru_cache(None)
@collect_with(set)
def get_signals_for_type(typ):
    for base in inspect.getmro(typ):
        for name in dir(base):
            if name.startswith("on_"):
                yield name
        # Only look at "real" attributes, to avoid triggering code in actual classes
        for name, value in base.__dict__.items():
            if hasattr(value, '_signal_handler_for'):
                yield name


class __FakeCode:
    def __init__(self, fake_filename, fake_firstlineno):
        self.co_filename = fake_filename
        self.co_firstlineno = fake_firstlineno


def __fake_meth_type(func, obj, name, code):
    method = func.__get__(obj, type(obj))
    method.__name__ = name
    method.__code__ = code
    return method


def register_object(obj, **kwargs):
    for method_name in get_signals_for_type(type(obj)):
        method = getattr(obj, method_name)
        assert callable(method), "'%s' (%r) is not callable" % (method, obj)

        # Don't use static/class methods for automatic event registration - they
        # could be registered multiple times if the are multiple objects of that
        # type in the system, or not at all if there are no objects of that type
        # in the system.
        assert method is not getattr(type(obj), method_name, None), "'%s' is a static/class method" % method

        signal_handler_for = getattr(method, '_signal_handler_for', None)

        method_name, *_ = method_name.partition("__")  # allows multiple methods for the same signal

        if not inspect.ismethod(method):
            # Fake method attributes for WeakMethod
            method.__self__ = obj
            method.__func__ = getattr(type(obj), method_name)
            if not hasattr(method, '__code__'):
                method.__code__ = __FakeCode('', '')
            fake_meth_type = functools.partial(
                __fake_meth_type,
                # Best effort: fake name and code for SignalHandler. We don't know the line number.
                name=method_name,
                code=__FakeCode(type(obj).__module__, '?'))

        method = weakref.WeakMethod(method)
        try:
            method._meth_type = fake_meth_type
        except UnboundLocalError:
            pass

        def params_with_kwargs(params):
            if kwargs:
                intersection = set(params).intersection(kwargs)
                assert not intersection, "parameter conflict in signal object registration (%s)" % (intersection)
                params = dict(params)
                params.update(kwargs)
            return params

        if signal_handler_for is None:  # import style signal
            params = getattr(method, '_signal_handler_params', {})
            register_signal(method_name, method, **params_with_kwargs(params))

        else:  # decoration style signals
            for signal, params in signal_handler_for.items():
                signal.register(method, **params_with_kwargs(params))


def unregister_object(obj):
    signals = set()
    for method_name in get_signals_for_type(type(obj)):
        method = getattr(obj, method_name)
        signal_handler_for = getattr(method, '_signal_handler_for', None)
        if signal_handler_for is None:  # import style signal
            signals.add(Signal.ALL[method_name.split('__', 1)[0]])
        else:
            signals.update(signal_handler_for.keys())
    for signal in signals:
        for handlers in signal.handlers.values():
            handlers[:] = (handler for handler in handlers if handler.bound_object is not obj)


def call_signal(name, **kwargs):
    return Signal(name)(**kwargs)


def log_all_signal_ids(logger=_logger, level=logging.DEBUG):
    for signal in sorted(Signal.ALL.itervalues(), key=lambda signal: signal.name):
        logger.log(level, "%s - '%s'", signal.id, signal.name)


# ===================================================================================================
# Module hack: ``from easypy.signals import on_some_signal``
#              ``from easypy.signals import on_ctx_some_signal`` for a context-manager signal
# ===================================================================================================
def _hack():
    import sys
    from types import ModuleType
    this = sys.modules[__name__]

    class SignalsModule(ModuleType):
        """The module-hack that allows us to use ``from easypy.signals import on_some_signal``"""
        __all__ = ()  # to make help() happy
        __package__ = __name__
        __path__ = []
        __file__ = __file__

        def __getattr__(self, name):
            try:
                return getattr(this, name)
            except AttributeError:
                if name.startswith("on_ctx_"):
                    return ContextManagerSignal(name)
                elif name.startswith("on_"):
                    return Signal(name)
                raise

        def __dir__(self):
            return sorted(dir(this) + list(Signal.ALL))

    module = SignalsModule(__name__, SignalsModule.__doc__)
    sys.modules[module.__name__] = module

_hack()
