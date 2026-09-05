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


class TaskNotScheduledError(TaskNotFoundError):
    """Deprecated alias of :class:`TaskNotFoundError`.

    quiv no longer raises this exception. The name stays exported so
    that imports in 0.x code keep working, and it subclasses
    :class:`TaskNotFoundError` so that code which still raises it is
    caught by ``except TaskNotFoundError``. Catch
    :class:`TaskNotFoundError` instead. Removed in 1.0.0.
    """


class TaskNotActiveError(QuivError):
    """Raised when an operation requires an ACTIVE task."""


class JobNotFoundError(QuivError):
    """Raised when a job record is not found."""
