"""Read-only GitHub check-run adapter for exact Phase 8 publication heads."""

from datetime import datetime
import re

from meta_loop.application.models import CheckRunConclusion, CheckRunObservation, CheckRunStatus
from meta_loop.domain.errors import ValidationError


class GitHubCheckSource:
    """Normalize current check runs for one exact commit SHA and nothing else."""

    _REPOSITORY = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
    _SHA = re.compile(r"^[0-9a-f]{40}$")

    def __init__(self, transport) -> None:
        self._transport = transport

    def read(self, publication_id: str, repository_name: str, head_sha: str) -> tuple[CheckRunObservation, ...]:
        if (not isinstance(repository_name, str) or self._REPOSITORY.fullmatch(repository_name) is None
                or not isinstance(head_sha, str) or self._SHA.fullmatch(head_sha) is None):
            raise ValidationError("GitHub check source input is invalid")

        payload = self._transport.get_json(
            f"/repos/{repository_name}/commits/{head_sha}/check-runs?per_page=100"
        )
        if not isinstance(payload, dict):
            raise ValidationError("GitHub check-run response is invalid")
        total = payload.get("total_count")
        runs = payload.get("check_runs")
        if (type(total) is not int or total < 0 or total > 100
                or not isinstance(runs, list) or len(runs) != total):
            raise ValidationError("GitHub check-run response is incomplete or invalid")

        observations = []
        for run in runs:
            observations.append(self._normalize(publication_id, head_sha, run))
        return tuple(observations)

    @classmethod
    def _normalize(cls, publication_id: str, head_sha: str, run) -> CheckRunObservation:
        if not isinstance(run, dict) or run.get("head_sha") != head_sha:
            raise ValidationError("GitHub check-run head does not match publication head")
        name = run.get("name")
        raw_status = run.get("status")
        if not isinstance(name, str):
            raise ValidationError("GitHub check-run name is invalid")
        try:
            status = CheckRunStatus(raw_status)
        except (TypeError, ValueError) as error:
            raise ValidationError("GitHub check-run status is invalid") from error

        if status is CheckRunStatus.COMPLETED:
            try:
                conclusion = CheckRunConclusion(run.get("conclusion"))
            except (TypeError, ValueError) as error:
                raise ValidationError("GitHub check-run conclusion is invalid") from error
            timestamp = run.get("completed_at")
        else:
            if run.get("conclusion") is not None:
                raise ValidationError("incomplete GitHub check-run cannot have a conclusion")
            conclusion = None
            timestamp = run.get("started_at")

        observed_at = cls._parse_time(timestamp)
        return CheckRunObservation(
            publication_id,
            head_sha,
            name,
            status,
            conclusion,
            observed_at,
        )

    @staticmethod
    def _parse_time(value) -> datetime:
        if not isinstance(value, str):
            raise ValidationError("GitHub check-run time is invalid")
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as error:
            raise ValidationError("GitHub check-run time is invalid") from error
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValidationError("GitHub check-run time is invalid")
        return parsed
