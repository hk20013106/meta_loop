class DomainError(Exception):
    """Base error for rejected domain operations."""


class ValidationError(DomainError):
    pass


class InvalidTransitionError(DomainError):
    pass


class OptimisticConflictError(DomainError):
    pass


class TaskNotFoundError(DomainError):
    pass


class CreateConflictError(DomainError):
    pass


class SequenceConflictError(DomainError):
    pass


class IdempotencyConflictError(ValidationError):
    pass


class QueueConflictError(DomainError):
    pass


class UnsupportedSchemaVersionError(DomainError):
    pass


class LeaseLostError(DomainError):
    pass


class UnsupportedGovernanceError(DomainError):
    pass
