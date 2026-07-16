from enum import Enum


class RiskClass(str, Enum):
    R0 = "R0"
    R1 = "R1"
    R2 = "R2"
    R3 = "R3"


class Role(str, Enum):
    SYSTEM = "system"
    VALIDATOR = "validator"
    PLANNER = "planner"
    DESIGN_REVIEWER = "design_reviewer"
    IMPLEMENTER = "implementer"
    PATCH_REVIEWER = "patch_reviewer"


class TaskStatus(str, Enum):
    RECEIVED = "received"
    VALIDATED = "validated"
    PLANNED = "planned"
    DESIGN_REVIEW = "design_review"
    IMPLEMENTING = "implementing"
    PREFLIGHT = "preflight"
    PATCH_REVIEW = "patch_review"
    READY_TO_PUBLISH = "ready_to_publish"
    PROPOSAL_READY = "proposal_ready"
    REWORK_REQUIRED = "rework_required"
    BLOCKED = "blocked"
    FAILED = "failed"
    COMPLETED = "completed"


class EventType(str, Enum):
    TASK_RECEIVED = "task_received"
    TASK_TRANSITIONED = "task_transitioned"
    TASK_BLOCKED = "task_blocked"
    REVIEW_REWORK_REQUIRED = "review_rework_required"
    ARTIFACT_REGISTERED = "artifact_registered"
    RUNNER_SESSION_REQUESTED = "runner_session_requested"
    WORKSPACE_ALLOCATED = "workspace_allocated"
    WORKSPACE_RELEASED = "workspace_released"
    SOURCE_INGESTED = "source_ingested"
