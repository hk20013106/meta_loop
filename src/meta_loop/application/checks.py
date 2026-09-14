from dataclasses import dataclass
from enum import Enum

from meta_loop.application.models import CheckRunConclusion, CheckRunObservation, CheckRunStatus
from meta_loop.domain.errors import ValidationError


class RequiredCheckStatus(str, Enum):
    PENDING = "pending"
    PASSED = "passed"
    FAILED = "failed"


@dataclass(frozen=True)
class RequiredCheckEvaluation:
    sha: str
    required_checks: tuple[str, ...]
    status: RequiredCheckStatus


class RequiredCheckEvaluator:
    """Single authority for evaluating one current exact-SHA check snapshot."""

    def __init__(self, required_checks: tuple[str, ...]) -> None:
        if (not isinstance(required_checks, tuple)
                or not required_checks
                or len(set(required_checks)) != len(required_checks)
                or any(not isinstance(name, str) or not name or len(name) > 128 for name in required_checks)):
            raise ValidationError("required checks configuration is invalid")
        self.required_checks = required_checks

    def evaluate(
        self,
        observations: tuple[CheckRunObservation, ...],
        exact_sha: str,
    ) -> RequiredCheckEvaluation:
        if (not isinstance(exact_sha, str)
                or len(exact_sha) != 40
                or any(character not in "0123456789abcdef" for character in exact_sha)):
            raise ValidationError("exact check SHA is invalid")
        if not isinstance(observations, tuple):
            raise ValidationError("check observations are invalid")

        current: dict[str, CheckRunObservation] = {}
        for observation in observations:
            if not isinstance(observation, CheckRunObservation) or observation.head_sha != exact_sha:
                raise ValidationError("check observation SHA does not match exact subject SHA")
            previous = current.get(observation.check_name)
            if previous is not None and previous.observed_at == observation.observed_at:
                raise ValidationError("check observation is ambiguous")
            if previous is None or observation.observed_at > previous.observed_at:
                current[observation.check_name] = observation

        required = [current.get(name) for name in self.required_checks]
        if any(observation is None or observation.status is not CheckRunStatus.COMPLETED for observation in required):
            status = RequiredCheckStatus.PENDING
        elif all(observation.conclusion is CheckRunConclusion.SUCCESS for observation in required):
            status = RequiredCheckStatus.PASSED
        else:
            status = RequiredCheckStatus.FAILED

        return RequiredCheckEvaluation(exact_sha, self.required_checks, status)
