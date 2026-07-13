"""Ordered SQL migration planning and ledger application."""

from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path

from meta_loop.domain.errors import ValidationError


@dataclass(frozen=True)
class Migration:
    version: str
    checksum: str
    sql: str


class MigrationRunner:
    def __init__(self, directory: Path) -> None:
        self._directory = directory

    @classmethod
    def from_directory(cls, directory: str | Path) -> "MigrationRunner":
        return cls(Path(directory))

    def plan(self) -> tuple[Migration, ...]:
        migrations = []
        for path in sorted(self._directory.glob("*.sql")):
            sql = path.read_text(encoding="utf-8")
            migrations.append(Migration(path.stem, sha256(sql.encode("utf-8")).hexdigest(), sql))
        return tuple(migrations)

    def validate_applied(self, versions: tuple[str, ...]) -> None:
        planned = tuple(migration.version for migration in self.plan())
        if versions != planned[:len(versions)]:
            raise ValidationError("applied migrations must be a contiguous prefix of the migration plan")

    def apply(self, connection: object) -> None:
        with connection.cursor() as cursor:
            cursor.execute("CREATE TABLE IF NOT EXISTS schema_migrations (version TEXT PRIMARY KEY, checksum TEXT NOT NULL, applied_at TIMESTAMPTZ NOT NULL DEFAULT now())")
            cursor.execute("SELECT version FROM schema_migrations ORDER BY version")
            self.validate_applied(tuple(row[0] for row in cursor.fetchall()))
            for migration in self.plan():
                cursor.execute("SELECT checksum FROM schema_migrations WHERE version = %s", (migration.version,))
                row = cursor.fetchone()
                if row is not None:
                    if row[0] != migration.checksum:
                        raise ValidationError(f"migration checksum mismatch: {migration.version}")
                    continue
                cursor.execute(migration.sql)
                cursor.execute("INSERT INTO schema_migrations (version, checksum) VALUES (%s, %s)", (migration.version, migration.checksum))
