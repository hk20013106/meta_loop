from dataclasses import dataclass
import re

from .errors import ValidationError


_SAFE_LOGICAL_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")


def validate_logical_name(value: object) -> None:
    if not isinstance(value, str) or not _SAFE_LOGICAL_NAME.fullmatch(value) or ".." in value:
        raise ValidationError("artifact logical name is invalid")


@dataclass(frozen=True)
class ArtifactDigest:
    value: str

    def __post_init__(self) -> None:
        if len(self.value) != 64 or any(char not in "0123456789abcdef" for char in self.value):
            raise ValidationError("artifact digest must be a lowercase SHA-256 hex digest")


@dataclass(frozen=True)
class ArtifactRef:
    digest: ArtifactDigest
    logical_name: str
    media_type: str
    schema_version: int = 1

    def __post_init__(self) -> None:
        validate_logical_name(self.logical_name)
        if not self.media_type or self.schema_version != 1:
            raise ValidationError("artifact reference is invalid or unsupported")

    def to_dict(self) -> dict[str, object]:
        return {
            "digest": f"sha256:{self.digest.value}",
            "logical_name": self.logical_name,
            "media_type": self.media_type,
            "schema_version": self.schema_version,
        }
