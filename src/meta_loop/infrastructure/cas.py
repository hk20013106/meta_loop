"""Local SHA-256 content-addressed storage with no catalog dependency."""

from hashlib import sha256
import os
from pathlib import Path
from tempfile import NamedTemporaryFile

from meta_loop.domain.artifacts import ArtifactDigest
from meta_loop.domain.errors import ValidationError


class FilesystemArtifactStore:
    def __init__(self, root: str | Path, repository_root: str | Path | None = None) -> None:
        self.root = Path(root).resolve()
        if repository_root is not None and self.root.is_relative_to(Path(repository_root).resolve()):
            raise ValidationError("CAS root cannot be inside the source repository")
        self.root.mkdir(parents=True, exist_ok=True)
        if os.name != "nt":
            os.chmod(self.root, 0o700)

    def _path(self, digest: str) -> Path:
        ArtifactDigest(digest)
        path = self.root / "sha256" / digest[:2] / digest[2:4] / digest
        if not path.resolve().is_relative_to(self.root):
            raise ValidationError("artifact path escapes CAS root")
        return path

    def put(self, chunks, expected_digest: str | None = None) -> tuple[str, int]:
        hasher, size = sha256(), 0
        staging = self.root / ".staging"
        staging.mkdir(exist_ok=True)
        with NamedTemporaryFile(mode="wb", dir=staging, delete=False) as output:
            temporary = Path(output.name)
            for chunk in chunks:
                if not isinstance(chunk, bytes):
                    raise ValidationError("artifact chunks must be bytes")
                output.write(chunk); hasher.update(chunk); size += len(chunk)
            output.flush(); os.fsync(output.fileno())
        digest = hasher.hexdigest()
        if expected_digest is not None and digest != expected_digest:
            temporary.unlink(missing_ok=True)
            raise ValidationError("artifact digest mismatch")
        target = self._path(digest)
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            temporary.unlink(missing_ok=True)
        else:
            os.replace(temporary, target)
            if os.name != "nt": os.chmod(target, 0o600)
        return digest, size

    def read(self, digest: str, verify: bool = True) -> bytes:
        path = self._path(digest)
        if path.is_symlink() or not path.is_file():
            raise ValidationError("artifact is missing or unsafe")
        value = path.read_bytes()
        if verify and sha256(value).hexdigest() != digest:
            raise ValidationError("artifact corruption detected")
        return value

    def scan_orphans(self, catalog_digests: tuple[str, ...]) -> tuple[str, ...]:
        known, found = set(catalog_digests), []
        base = self.root / "sha256"
        if not base.exists(): return ()
        for path in base.rglob("*"):
            if path.is_file() and not path.is_symlink() and path.name not in known:
                found.append(path.name)
        return tuple(sorted(found))
