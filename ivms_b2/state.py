from __future__ import annotations

import hashlib
import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import atomic_write_json
from .models import JobConfig


class JobStateStore:
    def __init__(self, job: JobConfig):
        common_identity = [
            job.device.host,
            str(job.camera.channel),
            job.start_date.isoformat(),
            job.end_date.isoformat(),
            job.start_time.isoformat(),
            job.end_time.isoformat(),
            str(job.business_days_only),
            "hikvision_device_wall_time_v2",
            job.cloud.endpoint,
            job.cloud.bucket,
            job.cloud.prefix,
        ]
        identity = "|".join([common_identity[0], common_identity[1], job.camera.camera_name, *common_identity[2:]])
        job_id = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16]
        self.path = job.spool_dir / f"job_{job_id}.json"
        self._lock = threading.Lock()
        audit_metadata = {
            "camera_name": job.camera.camera_name,
            "channel": job.camera.channel,
            "start_date": job.start_date.isoformat(),
            "end_date": job.end_date.isoformat(),
            "start_time": job.start_time.isoformat(timespec="minutes"),
            "end_time": job.end_time.isoformat(timespec="minutes"),
        }
        self.data: dict[str, Any] = {"version": 3, "job_id": job_id, "job": audit_metadata, "days": {}}
        source_path = self.path
        legacy_id = hashlib.sha256("|".join(common_identity).encode("utf-8")).hexdigest()[:16]
        legacy_path = job.spool_dir / f"job_{legacy_id}.json"
        if not source_path.exists() and legacy_path.exists():
            source_path = legacy_path
        if source_path.exists():
            try:
                loaded = json.loads(source_path.read_text(encoding="utf-8"))
                if loaded.get("job_id") in {job_id, legacy_id}:
                    self.data = loaded
                    self.data.update({"version": 3, "job_id": job_id, "job": audit_metadata})
            except (OSError, json.JSONDecodeError):
                pass

    def day(self, day_key: str) -> dict[str, Any]:
        return self.data.setdefault("days", {}).setdefault(day_key, {"status": "pendiente"})

    def update_day(self, day_key: str, **values: Any) -> dict[str, Any]:
        with self._lock:
            day = self.day(day_key)
            day.update(values)
            day["updated_at"] = datetime.now(timezone.utc).isoformat()
            atomic_write_json(self.path, self.data)
            return day

    def persist(self) -> None:
        with self._lock:
            atomic_write_json(self.path, self.data)
