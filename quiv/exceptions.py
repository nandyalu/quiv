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


class TaskNotScheduledError(QuivError):
    """Raised when an operation requires an existing scheduled task."""


class TaskNotFoundError(QuivError):
    """Raised when a task record is not found."""


class JobNotFoundError(QuivError):
    """Raised when a job record is not found."""
