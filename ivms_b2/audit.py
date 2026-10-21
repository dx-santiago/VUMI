from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any


LOG_RECORD = re.compile(
    r"^(?P<timestamp>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) "
    r"(?:(?P<camera>.+?) · )?(?P<day>\d{4}-\d{2}-\d{2}): (?P<detail>.+)$"
)

STATUS_LABELS = {
    "completado": "Completado",
    "sin_grabacion": "Sin grabación",
    "error": "Error",
    "cancelado": "Cancelado",
    "pendiente": "Pendiente",
    "descargando": "En proceso",
    "validando": "En proceso",
    "subiendo": "En proceso",
    "verificado": "En proceso",
}


@dataclass(frozen=True)
class AuditRecord:
    timestamp: datetime
    camera: str
    day: str
    status: str
    detail: str
    size: int = 0
    object_key: str = ""
    sha256: str = ""
    error: str = ""
    source: str = ""

    @property
    def size_text(self) -> str:
        if not self.size:
            return "—"
        return f"{self.size / 1024**3:.2f} GiB"


def load_audit_records(log_path: Path, spool_dir: Path) -> list[AuditRecord]:
    """Load the latest known result for each camera and recording day."""
    records: dict[tuple[str, str], AuditRecord] = {}
    for record in _log_records(log_path):
        records[(record.camera.casefold(), record.day)] = record
    for record in _state_records(spool_dir):
        key = (record.camera.casefold(), record.day)
        previous = records.get(key)
        same_event = previous is not None and previous.status == record.status and abs(
            (previous.timestamp - record.timestamp).total_seconds()
        ) <= 300
        if same_event and previous is not None:
            record = AuditRecord(
                timestamp=max(previous.timestamp, record.timestamp),
                camera=record.camera,
                day=record.day,
                status=record.status,
                detail=previous.detail,
                size=record.size,
                object_key=record.object_key,
                sha256=record.sha256,
                error=record.error or previous.error,
                source=record.source,
            )
        if previous is None or same_event or record.timestamp >= previous.timestamp:
            records[key] = record
    return sorted(records.values(), key=lambda item: item.timestamp, reverse=True)


def _log_records(path: Path) -> list[AuditRecord]:
    if not path.is_file():
        return []
    result: list[AuditRecord] = []
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    for line in lines:
        match = LOG_RECORD.match(line)
        if not match:
            continue
        detail = match.group("detail").strip()
        status = _terminal_status(detail)
        if not status:
            continue
        try:
            timestamp = datetime.strptime(match.group("timestamp"), "%Y-%m-%d %H:%M:%S")
        except ValueError:
            continue
        result.append(
            AuditRecord(
                timestamp=timestamp,
                camera=(match.group("camera") or "Sin identificar").strip(),
                day=match.group("day"),
                status=status,
                detail=detail,
                error=detail.partition(":")[2].strip() if status == "Error" else "",
                source=str(path),
            )
        )
    return result


def _state_records(spool_dir: Path) -> list[AuditRecord]:
    if not spool_dir.is_dir():
        return []
    result: list[AuditRecord] = []
    for path in spool_dir.glob("job_*.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            job = data.get("job") or {}
            camera = str(job.get("camera_name") or _infer_camera(data.get("days") or {})).strip()
            if not camera:
                continue
            fallback = datetime.fromtimestamp(path.stat().st_mtime)
            for day, raw in (data.get("days") or {}).items():
                if not isinstance(raw, dict):
                    continue
                status_key = str(raw.get("status") or "pendiente").lower()
                status = STATUS_LABELS.get(status_key, status_key.replace("_", " ").title())
                error = str(raw.get("error") or "")
                detail = error if error else status
                result.append(
                    AuditRecord(
                        timestamp=_local_datetime(raw.get("updated_at"), fallback),
                        camera=camera,
                        day=str(day),
                        status=status,
                        detail=detail,
                        size=_safe_int(raw.get("size")),
                        object_key=str(raw.get("object_key") or ""),
                        sha256=str(raw.get("sha256") or ""),
                        error=error,
                        source=str(path),
                    )
                )
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            continue
    return result


def _terminal_status(detail: str) -> str | None:
    normalized = detail.casefold()
    if normalized.startswith("completado"):
        return "Completado"
    if normalized.startswith("sin grabación") or normalized.startswith("sin grabacion"):
        return "Sin grabación"
    if normalized.startswith("error"):
        return "Error"
    if normalized.startswith("cancelado"):
        return "Cancelado"
    return None


def _local_datetime(value: Any, fallback: datetime) -> datetime:
    if not value:
        return fallback
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is not None:
            parsed = parsed.astimezone().replace(tzinfo=None)
        return parsed
    except ValueError:
        return fallback


def _safe_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _infer_camera(days: dict[str, Any]) -> str:
    """Recover a display name from legacy object keys that predate audit metadata."""
    for raw in days.values():
        if not isinstance(raw, dict):
            continue
        parts = str(raw.get("object_key") or "").replace("\\", "/").split("/")
        for index, part in enumerate(parts):
            if len(part) == 4 and part.isdigit() and index > 0:
                return parts[index - 1]
    return ""
