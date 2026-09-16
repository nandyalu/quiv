class QuivError(Exception):
    """Base exception for all Quiv errors."""


class ConfigurationError(QuivError):
    """Raised when scheduler configuration is invalid."""


class InvalidTimezoneError(ConfigurationError):
    """Raised when a timezone value cannot be resolved."""


class DatabaseInitializationError(QuivError):
    """Raised when scheduler database initialization fails."""


class HandlerRegistrationError(QuivError):
    """Raised when a task handler registration request is invalid."""


class HandlerNotRegisteredError(QuivError):
    """Raised when a handler is requested but not registered."""


class TaskNotFoundError(QuivError):
    """Raised when a task record is not found."""


class TaskNotActiveError(QuivError):
    """Raised when an operation requires an ACTIVE task."""


class JobNotFoundError(QuivError):
    """Raised when a job record is not found."""


class MainLoopUnavailableError(QuivError, RuntimeError):
    """Raised when ``run_on_main()`` cannot reach a main event loop.

    Either no active Quiv instance is registered, or the active Quiv has
    no resolvable main loop.

    It inherits :class:`RuntimeError` as well as :class:`QuivError`.
    quiv raised a bare ``RuntimeError`` here before 1.0.0, so an existing
    ``except RuntimeError`` clause keeps working, and ``except QuivError``
    now catches it too.
    """
