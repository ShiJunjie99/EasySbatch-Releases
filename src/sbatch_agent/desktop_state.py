"""Persistent, local-only preparation state for the Beta desktop application."""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import uuid

from .models import JobSpec
from .scanner_models import ProjectEvidence


class DesktopStateError(ValueError):
    """The desktop scan/preparation store is unavailable or inconsistent."""


class DesktopStateRepository:
    """Small SQLite store kept separate from immutable/submittable job records."""

    def __init__(self, path: Path):
        self.path = Path(path)
        try:
            self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            self.connection = sqlite3.connect(self.path, timeout=10)
            try:
                self.path.chmod(0o600)
            except OSError:
                self.connection.close()
                raise
            self.connection.row_factory = sqlite3.Row
            self.connection.execute("PRAGMA foreign_keys = ON")
            self.connection.execute("PRAGMA journal_mode = WAL")
            self.connection.executescript("""
                CREATE TABLE IF NOT EXISTS remote_scans (
                    id TEXT PRIMARY KEY,
                    project_dir TEXT NOT NULL,
                    captured_at TEXT NOT NULL,
                    evidence_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS desktop_metadata (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS preparations (
                    id TEXT PRIMARY KEY,
                    revision INTEGER NOT NULL,
                    state TEXT NOT NULL,
                    name TEXT,
                    software_id TEXT,
                    scan_id TEXT,
                    job_spec_json TEXT NOT NULL,
                    rendered_script TEXT,
                    review_sha256 TEXT,
                    warnings_json TEXT NOT NULL,
                    saved_record_id TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(scan_id) REFERENCES remote_scans(id)
                );
                CREATE INDEX IF NOT EXISTS preparations_updated_at
                    ON preparations(updated_at DESC);
            """)
        except sqlite3.Error as exc:
            raise DesktopStateError("desktop preparation state is unavailable") from exc

    def close(self) -> None:
        self.connection.close()

    def save_scan(self, evidence: ProjectEvidence) -> str:
        scan_id = str(uuid.uuid4())
        try:
            with self.connection:
                self.connection.execute(
                    "INSERT INTO remote_scans(id, project_dir, captured_at, evidence_json) VALUES (?, ?, ?, ?)",
                    (
                        scan_id, evidence.project_dir, evidence.scanned_at.isoformat(),
                        json.dumps(evidence.model_dump(mode="json"), ensure_ascii=False, separators=(",", ":")),
                    ),
                )
                self.connection.execute(
                    "INSERT INTO desktop_metadata(key, value) VALUES ('active_remote_scan', ?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (scan_id,),
                )
            return scan_id
        except sqlite3.Error as exc:
            raise DesktopStateError("remote scan evidence could not be saved") from exc

    def get_scan(self, scan_id: str) -> ProjectEvidence:
        if not isinstance(scan_id, str) or not scan_id:
            raise DesktopStateError("remote scan identifier is invalid")
        try:
            row = self.connection.execute(
                "SELECT evidence_json FROM remote_scans WHERE id = ?", (scan_id,),
            ).fetchone()
            if row is None:
                raise DesktopStateError("remote scan was not found")
            return ProjectEvidence.model_validate_json(row["evidence_json"])
        except (sqlite3.Error, ValueError, TypeError) as exc:
            if isinstance(exc, DesktopStateError):
                raise
            raise DesktopStateError("remote scan evidence is invalid") from exc

    def active_scan(self) -> tuple[str, ProjectEvidence] | None:
        try:
            row = self.connection.execute(
                "SELECT value FROM desktop_metadata WHERE key = 'active_remote_scan'",
            ).fetchone()
            if row is None:
                return None
            return row["value"], self.get_scan(row["value"])
        except sqlite3.Error as exc:
            raise DesktopStateError("remote scan evidence is unavailable") from exc

    def create_preparation(
        self, *, spec: JobSpec, name: str | None, software_id: str | None,
        scan_id: str | None, state: str, rendered_script: str | None,
        review_sha256: str | None, warnings: list[str],
    ) -> dict[str, object]:
        identifier = str(uuid.uuid4())
        now = datetime.now(timezone.utc).isoformat()
        try:
            with self.connection:
                self.connection.execute(
                    """INSERT INTO preparations(
                        id, revision, state, name, software_id, scan_id,
                        job_spec_json, rendered_script, review_sha256, warnings_json,
                        saved_record_id, created_at, updated_at
                    ) VALUES (?, 1, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?)""",
                    (
                        identifier, state, name, software_id, scan_id,
                        json.dumps(spec.model_dump(mode="json"), ensure_ascii=False, separators=(",", ":")),
                        rendered_script, review_sha256,
                        json.dumps(warnings, ensure_ascii=False, separators=(",", ":")),
                        now, now,
                    ),
                )
            return self.get_preparation(identifier)
        except sqlite3.IntegrityError as exc:
            raise DesktopStateError("the selected scan evidence no longer exists") from exc
        except sqlite3.Error as exc:
            raise DesktopStateError("preparation could not be saved") from exc

    def revise_preparation(
        self, identifier: str, *, expected_revision: int, spec: JobSpec,
        name: str | None, software_id: str | None, scan_id: str | None,
        state: str, rendered_script: str | None, review_sha256: str | None,
        warnings: list[str],
    ) -> dict[str, object]:
        if type(expected_revision) is not int or expected_revision < 1:
            raise DesktopStateError("preparation revision is invalid")
        now = datetime.now(timezone.utc).isoformat()
        try:
            with self.connection:
                result = self.connection.execute(
                    """UPDATE preparations SET
                        revision = revision + 1, state = ?, name = ?, software_id = ?, scan_id = ?,
                        job_spec_json = ?, rendered_script = ?, review_sha256 = ?, warnings_json = ?,
                        saved_record_id = NULL, updated_at = ?
                    WHERE id = ? AND revision = ?""",
                    (
                        state, name, software_id, scan_id,
                        json.dumps(spec.model_dump(mode="json"), ensure_ascii=False, separators=(",", ":")),
                        rendered_script, review_sha256,
                        json.dumps(warnings, ensure_ascii=False, separators=(",", ":")),
                        now, identifier, expected_revision,
                    ),
                )
                if result.rowcount != 1:
                    raise DesktopStateError("preparation changed; reload its latest revision")
            return self.get_preparation(identifier)
        except sqlite3.IntegrityError as exc:
            raise DesktopStateError("the selected scan evidence no longer exists") from exc
        except sqlite3.Error as exc:
            raise DesktopStateError("preparation could not be revised") from exc

    def mark_saved(self, identifier: str, *, expected_revision: int, record_id: str) -> dict[str, object]:
        now = datetime.now(timezone.utc).isoformat()
        try:
            with self.connection:
                result = self.connection.execute(
                    """UPDATE preparations SET state = 'SAVED', saved_record_id = ?, updated_at = ?
                    WHERE id = ? AND revision = ? AND state = 'READY_TO_SAVE'""",
                    (record_id, now, identifier, expected_revision),
                )
                if result.rowcount != 1:
                    raise DesktopStateError("preparation is not ready or changed; reload it")
            return self.get_preparation(identifier)
        except sqlite3.Error as exc:
            raise DesktopStateError("preparation could not be finalized") from exc

    def get_preparation(self, identifier: str) -> dict[str, object]:
        if not isinstance(identifier, str) or not identifier:
            raise DesktopStateError("preparation identifier is invalid")
        try:
            row = self.connection.execute(
                "SELECT * FROM preparations WHERE id = ?", (identifier,),
            ).fetchone()
        except sqlite3.Error as exc:
            raise DesktopStateError("preparation state is unavailable") from exc
        if row is None:
            raise DesktopStateError("preparation was not found")
        return self._view(row, detail=True)

    def list_preparations(self, *, limit: int) -> list[dict[str, object]]:
        if type(limit) is not int or not 1 <= limit <= 200:
            raise DesktopStateError("preparation list limit is invalid")
        try:
            rows = self.connection.execute(
                "SELECT * FROM preparations ORDER BY updated_at DESC LIMIT ?", (limit,),
            ).fetchall()
            return [self._view(row, detail=False) for row in rows]
        except sqlite3.Error as exc:
            raise DesktopStateError("preparation state is unavailable") from exc

    @staticmethod
    def _view(row: sqlite3.Row, *, detail: bool) -> dict[str, object]:
        try:
            spec = JobSpec.model_validate_json(row["job_spec_json"])
            warnings = json.loads(row["warnings_json"])
            if not isinstance(warnings, list) or any(not isinstance(value, str) for value in warnings):
                raise ValueError
        except (ValueError, TypeError) as exc:
            raise DesktopStateError("stored preparation data is invalid") from exc
        result: dict[str, object] = {
            "id": row["id"], "revision": row["revision"], "state": row["state"],
            "name": row["name"] or spec.job_name or "未命名智能草稿",
            "software_id": row["software_id"], "scan_id": row["scan_id"],
            "project_dir": spec.project_dir, "entrypoint": spec.entrypoint,
            "unresolved": [value.model_dump(mode="json") for value in spec.unresolved],
            "warnings": warnings, "saved_record_id": row["saved_record_id"],
            "created_at": row["created_at"], "updated_at": row["updated_at"],
        }
        if detail:
            result.update({
                "job_spec": spec.model_dump(mode="json"),
                "rendered_script": row["rendered_script"],
                "review_sha256": row["review_sha256"],
            })
        return result
