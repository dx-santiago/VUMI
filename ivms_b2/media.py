from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote, urlparse, urlunparse

from .models import DeviceConfig, Segment


class MediaError(RuntimeError):
    pass


def _format_eta(seconds: float) -> str:
    if seconds <= 0 or seconds > 7 * 24 * 3600:
        return "—"
    minutes = int(seconds // 60)
    if minutes == 0:
        return "<1 min"
    hours, minutes = divmod(minutes, 60)
    return f"{hours} h {minutes:02d} min" if hours else f"{minutes} min"


def find_binary(name: str, configured_dir: str = "") -> str:
    executable = name + (".exe" if os.name == "nt" else "")
    if configured_dir:
        for candidate in (Path(configured_dir) / executable, Path(configured_dir) / "bin" / executable):
            if candidate.is_file():
                return str(candidate)
    found = shutil.which(name)
    if not found:
        raise MediaError(f"No se encontró {executable}. Instale FFmpeg o configure su carpeta.")
    if os.name == "nt" and "chocolatey\\bin" in found.lower():
        chocolatey = Path(os.environ.get("ChocolateyInstall", r"C:\ProgramData\chocolatey"))
        native = chocolatey / "lib" / "ffmpeg" / "tools" / "ffmpeg" / "bin" / executable
        if native.is_file():
            return str(native)
    return found


def sha256_file(path: Path, chunk_size: int = 8 * 1024 * 1024, progress=None) -> str:
    digest = hashlib.sha256()
    total = path.stat().st_size
    processed = 0
    last_update = 0.0
    with path.open("rb") as handle:
        while chunk := handle.read(chunk_size):
            digest.update(chunk)
            processed += len(chunk)
            now = time.monotonic()
            if progress and (now - last_update >= 0.5 or processed == total):
                progress(processed, total)
                last_update = now
    return digest.hexdigest()


def _authorized_uri(uri: str, config: DeviceConfig, password: str) -> str:
    parsed = urlparse(uri)
    scheme = parsed.scheme or "rtsp"
    host = config.host
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    port = parsed.port or config.rtsp_port
    credentials = f"{quote(config.username, safe='')}:{quote(password, safe='')}@"
    return urlunparse(
        (scheme, f"{credentials}{host}:{port}", parsed.path, parsed.params, parsed.query, parsed.fragment)
    )


def _ffconcat_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace("'", "'\\''")


def _redact_error(value: str, config: DeviceConfig, password: str) -> str:
    redacted = value
    for secret in (config.username, password, quote(config.username, safe=""), quote(password, safe="")):
        if secret:
            redacted = redacted.replace(secret, "***")
    # FFmpeg suele imprimir la URL de entrada completa cuando falla RTSP.
    import re

    return re.sub(r"(rtsp://)[^@/\s]+@", r"\1***@", redacted, flags=re.IGNORECASE)


def download_day(
    segments: list[Segment],
    destination: Path,
    config: DeviceConfig,
    password: str,
    ffmpeg_dir: str = "",
    progress=None,
    cancel_event=None,
) -> None:
    if not segments:
        raise MediaError("No hay segmentos para descargar")
    ffmpeg = find_binary("ffmpeg", ffmpeg_dir)
    destination.parent.mkdir(parents=True, exist_ok=True)
    partial = destination.with_name(destination.stem + ".partial" + destination.suffix)
    playlist = ["ffconcat version 1.0"]
    for segment in segments:
        secured = _authorized_uri(segment.playback_uri, config, password)
        playlist.append(f"file '{_ffconcat_escape(secured)}'")
        # Esta opción debe pertenecer a cada entrada RTSP. Si se entrega en la
        # línea de comandos antes de pipe:0, FFmpeg intenta aplicarla al
        # demultiplexor concat y falla con "Option rtsp_transport not found".
        playlist.append("option rtsp_transport tcp")
    playlist_data = ("\n".join(playlist) + "\n").encode("utf-8")
    total_seconds = max(1.0, sum((item.end - item.start).total_seconds() for item in segments))
    command = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-progress",
        "pipe:2",
        "-f",
        "concat",
        "-safe",
        "0",
        "-protocol_whitelist",
        "file,pipe,tcp,tls,http,https,rtp,udp,rtsp,crypto",
        "-i",
        "pipe:0",
        "-map",
        "0:v:0",
        "-map",
        "0:a?",
        "-c:v",
        "libx264",
        "-preset",
        "fast",
        "-crf",
        "23",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-b:a",
        "128k",
        "-movflags",
        "+faststart",
        "-y",
        str(partial),
    ]
    creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    process = subprocess.Popen(
        command,
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        creationflags=creationflags,
    )
    assert process.stdin is not None
    assert process.stderr is not None
    try:
        process.stdin.write(playlist_data)
        process.stdin.close()
        errors: list[str] = []
        for raw in iter(process.stderr.readline, b""):
            line = raw.decode("utf-8", errors="replace").strip()
            if cancel_event is not None and cancel_event.is_set():
                process.terminate()
                raise MediaError("Descarga cancelada")
            if line.startswith("out_time_ms="):
                try:
                    seconds = int(line.split("=", 1)[1]) / 1_000_000
                    if progress:
                        progress(min(0.99, seconds / total_seconds))
                except ValueError:
                    pass
            elif line and "=" not in line:
                errors.append(_redact_error(line, config, password))
        return_code = process.wait()
        if return_code != 0:
            detail = " | ".join(errors[-5:])
            raise MediaError(f"FFmpeg no pudo crear el video diario: {detail or return_code}")
        if not partial.exists() or partial.stat().st_size == 0:
            raise MediaError("FFmpeg finalizó sin producir un archivo")
        os.replace(partial, destination)
        if progress:
            progress(1.0)
    except Exception:
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=10)
        raise


def download_day_http(
    segments: list[Segment],
    destination: Path,
    isapi_client,
    ffmpeg_dir: str = "",
    progress=None,
    cancel_event=None,
) -> None:
    """Descarga archivos a máxima velocidad y los remultiplexa sin recodificar."""
    if not segments:
        raise MediaError("No hay segmentos para descargar")
    ffmpeg = find_binary("ffmpeg", ffmpeg_dir)
    destination.parent.mkdir(parents=True, exist_ok=True)
    segment_dir = destination.parent / f".{destination.stem}_segments"
    segment_dir.mkdir(parents=True, exist_ok=True)
    expected_total = sum(item.size or 0 for item in segments)
    completed_bytes = sum(
        (item.size or 0)
        for index, item in enumerate(segments, start=1)
        if (segment_dir / f"clip_{index:04d}.mp4").is_file()
    )
    initial_completed = completed_bytes
    started = time.monotonic()

    for index, segment in enumerate(segments, start=1):
        if cancel_event is not None and cancel_event.is_set():
            raise MediaError("Descarga cancelada")
        segment_path = segment_dir / f"segment_{index:04d}.ps"
        clip_path = segment_dir / f"clip_{index:04d}.mp4"
        if clip_path.exists() and clip_path.stat().st_size > 1024:
            continue
        partial = segment_path.with_suffix(".ps.part")
        if not segment_path.exists() or segment_path.stat().st_size <= 1024:
            if partial.exists():
                partial.unlink()
            attempts = 3
            for attempt in range(1, attempts + 1):
                try:
                    with isapi_client.open_download(segment.playback_uri) as response:
                        segment_expected = int(response.headers.get("Content-Length", 0) or 0)
                        received = 0
                        last_update = 0.0
                        with partial.open("wb") as handle:
                            for chunk in response.iter_content(chunk_size=4 * 1024 * 1024):
                                if cancel_event is not None and cancel_event.is_set():
                                    raise MediaError("Descarga cancelada")
                                if not chunk:
                                    continue
                                handle.write(chunk)
                                received += len(chunk)
                                now = time.monotonic()
                                if progress and now - last_update >= 0.5:
                                    transferred = completed_bytes + received
                                    elapsed = max(0.1, now - started)
                                    session_bytes = max(0, transferred - initial_completed)
                                    speed = session_bytes / elapsed
                                    total = expected_total or (completed_bytes + segment_expected)
                                    fraction = min(0.99, transferred / total) if total else 0.0
                                    eta = (total - transferred) / speed if speed > 0 and total else 0
                                    detail = (
                                        f"Segmento {index}/{len(segments)} · "
                                        f"{transferred / 1024**3:.2f}/{total / 1024**3:.2f} GiB · "
                                        f"{speed / 1024**2:.2f} MiB/s · ETA {_format_eta(eta)}"
                                    )
                                    progress(fraction, detail)
                                    last_update = now
                    if segment_expected and received != segment_expected:
                        raise MediaError(
                            f"Segmento {index} incompleto: {received} de {segment_expected} bytes"
                        )
                    if received <= 1024:
                        raise MediaError(f"El segmento {index} está vacío")
                    os.replace(partial, segment_path)
                    break
                except Exception as exc:
                    if isinstance(exc, MediaError) and "cancelad" in str(exc).lower():
                        raise
                    if attempt == attempts:
                        raise MediaError(
                            f"No fue posible descargar el segmento {index} después de {attempts} intentos: {exc}"
                        ) from exc
                    time.sleep(2**attempt)

        if progress:
            progress(
                min(0.99, completed_bytes / expected_total) if expected_total else 0,
                f"Preparando segmento {index}/{len(segments)} sin recodificar",
            )
        source_start = segment.source_start or segment.start
        offset = max(0.0, (segment.start - source_start).total_seconds())
        duration = max(0.1, (segment.end - segment.start).total_seconds())
        clip_partial = clip_path.with_suffix(".partial.mp4")
        trim_command = [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-ss",
            f"{offset:.3f}",
            "-i",
            str(segment_path),
            "-t",
            f"{duration:.3f}",
            "-map",
            "0:v:0",
            "-map",
            "0:a?",
            "-c:v",
            "copy",
            "-c:a",
            "aac",
            "-b:a",
            "128k",
            "-avoid_negative_ts",
            "make_zero",
            "-movflags",
            "+faststart",
            "-y",
            str(clip_partial),
        ]
        creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        trimmed = subprocess.run(trim_command, capture_output=True, creationflags=creationflags)
        if trimmed.returncode != 0 or not clip_partial.exists() or clip_partial.stat().st_size <= 1024:
            detail = trimmed.stderr.decode("utf-8", errors="replace")[-1000:]
            raise MediaError(f"No fue posible preparar el segmento {index}: {detail}")
        os.replace(clip_partial, clip_path)
        segment_path.unlink()
        completed_bytes += segment.size or clip_path.stat().st_size

    if progress:
        progress(0.99, "Descarga completa · creando MP4 sin recodificar")
    playlist = segment_dir / "segments.ffconcat"
    lines = ["ffconcat version 1.0"]
    for index in range(1, len(segments) + 1):
        path = (segment_dir / f"clip_{index:04d}.mp4").resolve()
        normalized = str(path).replace("\\", "/")
        lines.append(f"file '{_ffconcat_escape(normalized)}'")
    playlist.write_text("\n".join(lines) + "\n", encoding="utf-8")
    partial_output = destination.with_name(destination.stem + ".partial" + destination.suffix)
    command = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "warning",
        "-progress",
        "pipe:1",
        "-fflags",
        "+genpts",
        "-f",
        "concat",
        "-safe",
        "0",
        "-i",
        str(playlist),
        "-map",
        "0:v:0",
        "-map",
        "0:a?",
        "-c",
        "copy",
        "-avoid_negative_ts",
        "make_zero",
        "-movflags",
        "+faststart",
        "-y",
        str(partial_output),
    ]
    creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    process = subprocess.Popen(
        command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        creationflags=creationflags,
    )
    assert process.stdout is not None
    messages: list[str] = []
    for raw in iter(process.stdout.readline, b""):
        if cancel_event is not None and cancel_event.is_set():
            process.terminate()
            process.wait(timeout=10)
            raise MediaError("Procesamiento cancelado; los segmentos descargados se conservaron")
        line = raw.decode("utf-8", errors="replace").strip()
        if line and "=" not in line:
            messages.append(line)
            messages = messages[-10:]
    return_code = process.wait()
    if return_code != 0 or not partial_output.exists() or partial_output.stat().st_size <= 1024:
        raise MediaError(
            "FFmpeg no pudo unir los segmentos sin recodificar: "
            + (" | ".join(messages[-5:]) or str(return_code))
        )
    os.replace(partial_output, destination)
    # Solo se elimina la carpeta exacta creada para este MP4.
    if segment_dir.parent.resolve() != destination.parent.resolve() or not segment_dir.name.endswith("_segments"):
        raise MediaError("Se rechazó la limpieza de una carpeta temporal inesperada")
    shutil.rmtree(segment_dir)
    if progress:
        progress(1.0, "MP4 diario creado sin recodificación")


def probe_media(path: Path, ffmpeg_dir: str = "") -> dict:
    ffprobe = find_binary("ffprobe", ffmpeg_dir)
    command = [
        ffprobe,
        "-v",
        "error",
        "-show_format",
        "-show_streams",
        "-of",
        "json",
        str(path),
    ]
    creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    result = subprocess.run(command, capture_output=True, creationflags=creationflags, timeout=120)
    if result.returncode != 0:
        raise MediaError("FFprobe determinó que el archivo no es reproducible")
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise MediaError("FFprobe devolvió una respuesta inválida") from exc
    streams = data.get("streams", [])
    if not any(item.get("codec_type") == "video" for item in streams):
        raise MediaError("El archivo no contiene una pista de video")
    duration = float(data.get("format", {}).get("duration", 0) or 0)
    if duration <= 0 or path.stat().st_size <= 0:
        raise MediaError("El video tiene duración o tamaño inválidos")
    return {
        "duration": duration,
        "size": path.stat().st_size,
        "format": data.get("format", {}).get("format_name", ""),
        "video_codec": next(
            (item.get("codec_name", "") for item in streams if item.get("codec_type") == "video"), ""
        ),
        "audio_codec": next(
            (item.get("codec_name", "") for item in streams if item.get("codec_type") == "audio"), ""
        ),
    }


def missing_intervals(segments: list[Segment], start: datetime, end: datetime) -> list[dict[str, str]]:
    if not segments:
        return [{"start": start.isoformat(), "end": end.isoformat()}]
    start_utc = start.astimezone(timezone.utc)
    end_utc = end.astimezone(timezone.utc)
    cursor = start_utc
    gaps: list[dict[str, str]] = []
    for item in sorted(segments, key=lambda value: value.start):
        item_start = item.start.astimezone(timezone.utc)
        item_end = item.end.astimezone(timezone.utc)
        if item_end <= cursor:
            continue
        if item_start > cursor and (item_start - cursor).total_seconds() > 2:
            gaps.append({"start": cursor.isoformat(), "end": min(item_start, end_utc).isoformat()})
        cursor = max(cursor, item_end)
        if cursor >= end_utc:
            break
    if cursor < end_utc and (end_utc - cursor).total_seconds() > 2:
        gaps.append({"start": cursor.isoformat(), "end": end_utc.isoformat()})
    return gaps
