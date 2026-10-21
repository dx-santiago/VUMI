from __future__ import annotations

import json
import os
import shutil
import threading
import time
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .cloud import B2MultipartClient, object_key, safe_component
from .isapi import IsapiClient, recording_name
from .media import download_day_http, missing_intervals, probe_media, sha256_file
from .models import BatchProgressCallback, JobConfig, ProgressCallback, Segment
from .state import JobStateStore


class JobRunner:
    def __init__(
        self,
        job: JobConfig,
        nvr_password: str,
        b2_key_id: str,
        b2_application_key: str,
        ffmpeg_dir: str = "",
        callback: ProgressCallback | None = None,
        cancel_event: threading.Event | None = None,
    ):
        self.job = job
        self.nvr_password = nvr_password
        self.ffmpeg_dir = ffmpeg_dir
        self.callback = callback or (lambda *_: None)
        self.cancel_event = cancel_event or threading.Event()
        self.isapi = IsapiClient(job.device, nvr_password)
        self.cloud = B2MultipartClient(job.cloud, b2_key_id, b2_application_key)
        self.state = JobStateStore(job)

    def run(self) -> dict[str, Any]:
        self.job.spool_dir.mkdir(parents=True, exist_ok=True)
        windows = self.job.windows()
        result: dict[str, Any] = {"completed": 0, "empty": 0, "errors": 0, "cancelled": 0, "days": {}}
        for index, window in enumerate(windows):
            if self.cancel_event.is_set():
                break
            day_key = window.day.isoformat()
            try:
                status = self._run_day(window, index, len(windows))
                result["empty" if status == "sin_grabacion" else "completed"] += 1
                result["days"][day_key] = status
            except Exception as exc:
                if self.cancel_event.is_set():
                    result["cancelled"] += 1
                    result["days"][day_key] = "cancelado"
                    self.state.update_day(day_key, status="cancelado", error=str(exc))
                    self.callback(day_key, "Cancelado", None)
                    break
                result["errors"] += 1
                result["days"][day_key] = "error"
                self.state.update_day(day_key, status="error", error=str(exc))
                self.callback(day_key, f"Error: {exc}", None)
        return result

    def _run_day(self, window, day_index: int, day_count: int) -> str:
        day_key = window.day.isoformat()
        day_state = self.state.day(day_key)
        key = object_key(
            self.job.cloud.prefix,
            self.job.camera.camera_name,
            window.day,
            self.job.start_time,
            self.job.end_time,
            self.job.camera.storage_source,
        )
        filename = key.rsplit("/", 1)[-1]
        if self.job.camera.storage_source:
            filename = f"{safe_component(self.job.camera.storage_source)}_{filename}"
        local_path = self.job.spool_dir / filename
        self.state.update_day(day_key, object_key=key, local_path=str(local_path), error="")

        if day_state.get("status") == "completado" and not local_path.exists():
            if self.cloud.remote_matches(key, int(day_state.get("size", 0)), day_state.get("sha256", "")):
                self.callback(day_key, "Completado anteriormente", 1.0)
                return "completado"
            raise RuntimeError("El estado local indica completado, pero el objeto remoto no coincide")

        self.callback(day_key, "Buscando grabaciones", 0.0)
        segments = self.isapi.search_recordings(window.start, window.end, self.job.camera.channel)
        if not segments:
            self.state.update_day(
                day_key,
                status="sin_grabacion",
                segments=[],
                missing_intervals=[{"start": window.start.isoformat(), "end": window.end.isoformat()}],
            )
            self.callback(day_key, "Sin grabación", 1.0)
            return "sin_grabacion"

        # La búsqueda recorta el primer/último resultado al rango solicitado,
        # pero /download entrega el archivo físico completo. Una búsqueda
        # ampliada permite calcular el offset exacto que se debe conservar.
        wide_segments = self.isapi.search_recordings(
            window.start - timedelta(days=1), window.end + timedelta(days=1), self.job.camera.channel
        )
        physical_by_name = {
            recording_name(item.playback_uri): item
            for item in wide_segments
            if recording_name(item.playback_uri)
        }
        resolved_segments = []
        for item in segments:
            physical = physical_by_name.get(recording_name(item.playback_uri))
            resolved_segments.append(
                replace(
                    item,
                    source_start=physical.start if physical else item.start,
                    source_end=physical.end if physical else item.end,
                )
            )
        segments = resolved_segments

        reported_size = sum(item.size or 0 for item in segments)
        # Algunos firmwares omiten size en playbackURI. Se usa 16 Mbit/s como
        # reserva conservadora para no comenzar un día sin espacio suficiente.
        fallback_size = int(sum((item.end - item.start).total_seconds() for item in segments) * 16_000_000 / 8)
        expected_size = reported_size or fallback_size
        gaps = missing_intervals(segments, window.start, window.end)
        serialized = [
            {
                "start": item.start.isoformat(),
                "end": item.end.isoformat(),
                "size": item.size,
                "source_start": item.source_start.isoformat() if item.source_start else None,
                "source_end": item.source_end.isoformat() if item.source_end else None,
            }
            for item in segments
        ]
        self.state.update_day(
            day_key,
            status="pendiente",
            expected_size=expected_size,
            segments=serialized,
            missing_intervals=gaps,
        )
        self._check_disk(expected_size)

        valid_local = False
        if local_path.exists() and local_path.stat().st_size > 0:
            try:
                probe_media(local_path, self.ffmpeg_dir)
                valid_local = True
            except Exception:
                valid_local = False
        if not valid_local:
            self.state.update_day(day_key, status="descargando")
            self.callback(day_key, "Descargando y convirtiendo", 0.0)
            download_day_http(
                segments,
                local_path,
                self.isapi,
                self.ffmpeg_dir,
                progress=lambda value, detail: self.callback(day_key, detail, value),
                cancel_event=self.cancel_event,
            )

        self.state.update_day(day_key, status="validando")
        self.callback(day_key, "Validando MP4", None)
        probe = probe_media(local_path, self.ffmpeg_dir)
        size = local_path.stat().st_size
        hash_started = time.monotonic()

        def hash_progress(processed: int, total: int) -> None:
            elapsed = max(0.1, time.monotonic() - hash_started)
            speed = processed / elapsed
            eta = (total - processed) / speed if speed else 0
            self.callback(
                day_key,
                f"SHA-256 · {processed / 1024**3:.2f}/{total / 1024**3:.2f} GiB · "
                f"{speed / 1024**2:.1f} MiB/s · ETA {self._eta(eta)}",
                processed / total if total else None,
            )

        digest = sha256_file(local_path, progress=hash_progress)
        self.state.update_day(day_key, size=size, sha256=digest)

        multipart = self.state.day(day_key).setdefault("multipart", {})
        self.state.update_day(day_key, status="subiendo")
        self.callback(day_key, "Subiendo a Backblaze", 0.0)
        upload_started = time.monotonic()

        def upload_progress(value: float) -> None:
            elapsed = max(0.1, time.monotonic() - upload_started)
            transferred = size * value
            speed = transferred / elapsed
            eta = (size - transferred) / speed if speed else 0
            self.callback(
                day_key,
                f"Subiendo B2 · {value * 100:.1f}% · {transferred / 1024**3:.2f}/{size / 1024**3:.2f} GiB · "
                f"{speed / 1024**2:.1f} MiB/s · ETA {self._eta(eta)}",
                value,
            )

        self.cloud.upload_file(
            local_path,
            key,
            digest,
            multipart,
            self.state.persist,
            progress=upload_progress,
            cancel_event=self.cancel_event,
        )

        self.state.update_day(day_key, status="verificado")
        self.callback(day_key, "Verificando objeto remoto", None)
        self.cloud.verify(local_path, key, digest)
        manifest = {
            "version": 1,
            "status": "completed",
            "dvr": self.job.camera.source_name,
            "camera": self.job.camera.camera_name,
            "channel": self.job.camera.channel,
            "requested_start": window.start.isoformat(),
            "requested_end": window.end.isoformat(),
            "segments": serialized,
            "missing_intervals": gaps,
            "object_key": key,
            "size": size,
            "sha256": digest,
            "media": probe,
            "uploaded_at": datetime.now(timezone.utc).isoformat(),
        }
        self.cloud.upload_manifest(key, manifest)
        self._safe_delete(local_path)
        self.state.update_day(day_key, status="completado", multipart={}, error="")
        self.callback(day_key, "Completado", 1.0)
        return "completado"

    def _check_disk(self, expected_size: int) -> None:
        free = shutil.disk_usage(self.job.spool_dir).free
        reserve = 10 * 1024**3
        # Durante el remux coexisten los segmentos originales y el MP4 final.
        required = int(expected_size * 2.15) + reserve if expected_size else reserve
        if free < required:
            raise RuntimeError(
                f"Espacio insuficiente: se requieren {required / 1024**3:.2f} GiB y hay {free / 1024**3:.2f} GiB"
            )

    def _safe_delete(self, path: Path) -> None:
        spool = self.job.spool_dir.resolve()
        target = path.resolve()
        if target.parent != spool or not target.is_file() or target.suffix.lower() != ".mp4":
            raise RuntimeError("Se rechazó el borrado de una ruta temporal inesperada")
        target.unlink()

    @staticmethod
    def _eta(seconds: float) -> str:
        if seconds <= 0:
            return "—"
        minutes = int(seconds // 60)
        if minutes == 0:
            return "<1 min"
        hours, minutes = divmod(minutes, 60)
        return f"{hours} h {minutes:02d} min" if hours else f"{minutes} min"


class BatchJobRunner:
    """Ejecuta trabajos de una cámara en serie y agrega sus resultados."""

    def __init__(
        self,
        jobs: list[JobConfig],
        nvr_password: str | dict[str, str],
        b2_key_id: str,
        b2_application_key: str,
        ffmpeg_dir: str = "",
        callback: BatchProgressCallback | None = None,
        cancel_event: threading.Event | None = None,
        runner_class=JobRunner,
    ):
        self.jobs = jobs
        self.nvr_password = nvr_password
        self.b2_key_id = b2_key_id
        self.b2_application_key = b2_application_key
        self.ffmpeg_dir = ffmpeg_dir
        self.callback = callback or (lambda *_: None)
        self.cancel_event = cancel_event or threading.Event()
        self.runner_class = runner_class

    def run(self) -> dict[str, Any]:
        totals = {"completed": 0, "empty": 0, "errors": 0, "cancelled": 0}
        by_camera: dict[str, dict[str, Any]] = {}
        for index, job in enumerate(self.jobs):
            task_count = len(job.windows())
            if self.cancel_event.is_set():
                totals["cancelled"] += sum(len(item.windows()) for item in self.jobs[index:])
                break
            camera = job.camera
            password = (
                self.nvr_password.get(camera.source_id, "")
                if isinstance(self.nvr_password, dict)
                else self.nvr_password
            )
            if not password:
                raise ValueError(f"Falta la contraseña guardada para {camera.source_name or camera.camera_name}")
            runner = self.runner_class(
                job,
                password,
                self.b2_key_id,
                self.b2_application_key,
                self.ffmpeg_dir,
                lambda day, detail, value, current=camera: self.callback(current, day, detail, value),
                self.cancel_event,
            )
            result = runner.run()
            processed = sum(result.get(key, 0) for key in ("completed", "empty", "errors", "cancelled"))
            cancelled = result.get("cancelled", 0)
            if self.cancel_event.is_set():
                cancelled += max(0, task_count - processed)
            camera_key = f"{camera.source_id}:{camera.channel}" if camera.source_id else str(camera.channel)
            by_camera[camera_key] = {**result, "cancelled": cancelled, "name": camera.display_name}
            for key in ("completed", "empty", "errors"):
                totals[key] += result.get(key, 0)
            totals["cancelled"] += cancelled
        return {**totals, "by_camera": by_camera}
