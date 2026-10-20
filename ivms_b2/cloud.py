from __future__ import annotations

import base64
import hashlib
import json
import math
import re
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

try:
    import boto3
    from botocore.config import Config
    from botocore.exceptions import ClientError
except ImportError:  # pragma: no cover
    boto3 = None
    Config = None
    ClientError = Exception

from .models import CameraConfig, CloudConfig


class CloudError(RuntimeError):
    pass


class ObjectConflictError(CloudError):
    pass


def normalize_endpoint(value: str) -> str:
    raw = value.strip()
    if not raw:
        raise ValueError("El endpoint de Backblaze es obligatorio")
    if "://" not in raw:
        raw = "https://" + raw
    parsed = urlparse(raw)
    if parsed.scheme.lower() != "https" or not parsed.hostname:
        raise ValueError("El endpoint debe ser una dirección HTTPS válida")
    return f"https://{parsed.netloc}".rstrip("/")


def safe_component(value: str) -> str:
    cleaned = re.sub(r"[^\w.-]+", "_", value.strip(), flags=re.UNICODE).strip("._")
    return cleaned or "camara"


def validate_cameras(cameras: list[CameraConfig]) -> None:
    if not cameras:
        raise ValueError("Seleccione al menos una cámara")
    channels: set[int] = set()
    names: set[str] = set()
    for camera in cameras:
        if camera.channel <= 0:
            raise ValueError("Los canales deben ser enteros positivos")
        if not camera.camera_name.strip():
            raise ValueError(f"Complete el nombre de la cámara del canal {camera.channel}")
        normalized = safe_component(camera.camera_name).casefold()
        if camera.channel in channels:
            raise ValueError(f"El canal {camera.channel} está repetido")
        if normalized in names:
            raise ValueError("Dos cámaras seleccionadas producirían la misma ruta en Backblaze")
        channels.add(camera.channel)
        names.add(normalized)


def object_key(prefix: str, camera: str, day, start_time, end_time) -> str:
    root = prefix.strip("/")
    name = safe_component(camera)
    filename = f"{name}_{day.isoformat()}_{start_time:%H%M}-{end_time:%H%M}.mp4"
    pieces = [part for part in [root, name, f"{day:%Y}", f"{day:%m}", f"{day:%d}", filename] if part]
    return "/".join(pieces)


class B2MultipartClient:
    def __init__(self, config: CloudConfig, key_id: str, application_key: str):
        if boto3 is None:
            raise CloudError("Falta instalar boto3 y botocore")
        self.config = config
        self.endpoint = normalize_endpoint(config.endpoint)
        self.client = boto3.client(
            "s3",
            endpoint_url=self.endpoint,
            aws_access_key_id=key_id,
            aws_secret_access_key=application_key,
            config=Config(signature_version="s3v4", retries={"max_attempts": 10, "mode": "adaptive"}),
        )

    def test_connection(self) -> dict[str, Any]:
        self.client.head_bucket(Bucket=self.config.bucket)
        return {"endpoint": self.endpoint, "bucket": self.config.bucket}

    def head(self, key: str) -> dict[str, Any] | None:
        try:
            return self.client.head_object(Bucket=self.config.bucket, Key=key)
        except ClientError as exc:
            code = str(exc.response.get("Error", {}).get("Code", ""))
            status = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
            if code in {"404", "NoSuchKey", "NotFound"} or status == 404:
                return None
            raise

    def remote_matches(self, key: str, size: int, sha256: str) -> bool:
        remote = self.head(key)
        if remote is None:
            return False
        metadata = {k.lower(): v for k, v in remote.get("Metadata", {}).items()}
        return int(remote.get("ContentLength", -1)) == size and metadata.get("sha256") == sha256

    def upload_file(
        self,
        path: Path,
        key: str,
        sha256: str,
        multipart_state: dict[str, Any],
        persist: Callable[[], None],
        progress=None,
        cancel_event=None,
    ) -> None:
        size = path.stat().st_size
        existing = self.head(key)
        if existing is not None:
            if self.remote_matches(key, size, sha256):
                if progress:
                    progress(1.0)
                return
            raise ObjectConflictError(f"Ya existe un objeto diferente en {key}")

        part_size = max(5, self.config.part_size_mib) * 1024 * 1024
        part_count = math.ceil(size / part_size)
        if part_count > 10000:
            raise CloudError("El tamaño de parte produce más de 10.000 partes")
        if multipart_state.get("sha256") != sha256 or multipart_state.get("key") != key:
            multipart_state.clear()
        upload_id = multipart_state.get("upload_id")
        if not upload_id:
            response = self.client.create_multipart_upload(
                Bucket=self.config.bucket,
                Key=key,
                ContentType="video/mp4",
                Metadata={"sha256": sha256},
                ServerSideEncryption="AES256",
            )
            upload_id = response["UploadId"]
            multipart_state.update({"upload_id": upload_id, "key": key, "sha256": sha256, "parts": {}})
            persist()

        server_parts = self._list_parts(key, upload_id)
        if server_parts is None:
            multipart_state.clear()
            persist()
            return self.upload_file(
                path,
                key,
                sha256,
                multipart_state,
                persist,
                progress,
                cancel_event,
            )
        completed: dict[int, str] = {int(number): etag for number, etag in server_parts.items()}
        multipart_state["parts"] = {str(number): etag for number, etag in completed.items()}
        persist()
        lock = threading.Lock()

        def upload(number: int) -> tuple[int, str]:
            if cancel_event is not None and cancel_event.is_set():
                raise CloudError("Subida cancelada")
            offset = (number - 1) * part_size
            length = min(part_size, size - offset)
            with path.open("rb") as handle:
                handle.seek(offset)
                body = handle.read(length)
            md5 = base64.b64encode(hashlib.md5(body).digest()).decode("ascii")
            response = self.client.upload_part(
                Bucket=self.config.bucket,
                Key=key,
                UploadId=upload_id,
                PartNumber=number,
                Body=body,
                ContentMD5=md5,
            )
            return number, response["ETag"]

        pending = [number for number in range(1, part_count + 1) if number not in completed]
        if progress:
            progress(len(completed) / part_count)
        with ThreadPoolExecutor(max_workers=max(1, min(4, self.config.concurrency))) as executor:
            futures = {executor.submit(upload, number): number for number in pending}
            for future in as_completed(futures):
                number, etag = future.result()
                with lock:
                    completed[number] = etag
                    multipart_state["parts"][str(number)] = etag
                    persist()
                    if progress:
                        progress(len(completed) / part_count)
                if cancel_event is not None and cancel_event.is_set():
                    for pending_future in futures:
                        pending_future.cancel()
                    raise CloudError("Subida cancelada")

        parts = [{"PartNumber": number, "ETag": completed[number]} for number in sorted(completed)]
        self.client.complete_multipart_upload(
            Bucket=self.config.bucket,
            Key=key,
            UploadId=upload_id,
            MultipartUpload={"Parts": parts},
        )
        multipart_state.clear()
        persist()

    def _list_parts(self, key: str, upload_id: str) -> dict[int, str] | None:
        parts: dict[int, str] = {}
        marker = 0
        try:
            while True:
                response = self.client.list_parts(
                    Bucket=self.config.bucket,
                    Key=key,
                    UploadId=upload_id,
                    PartNumberMarker=marker,
                )
                for item in response.get("Parts", []):
                    parts[int(item["PartNumber"])] = item["ETag"]
                if not response.get("IsTruncated"):
                    break
                marker = int(response.get("NextPartNumberMarker", marker))
        except ClientError as exc:
            code = str(exc.response.get("Error", {}).get("Code", ""))
            if code in {"NoSuchUpload", "404"}:
                return None
            raise
        return parts

    def verify(self, path: Path, key: str, sha256: str, sample_size: int = 1024 * 1024) -> None:
        size = path.stat().st_size
        if not self.remote_matches(key, size, sha256):
            raise CloudError("El tamaño o SHA-256 remoto no coincide")
        ranges = [(0, min(size, sample_size) - 1)]
        if size > sample_size:
            ranges.append((max(0, size - sample_size), size - 1))
        with path.open("rb") as handle:
            for start, end in ranges:
                handle.seek(start)
                local = handle.read(end - start + 1)
                response = self.client.get_object(
                    Bucket=self.config.bucket,
                    Key=key,
                    Range=f"bytes={start}-{end}",
                )
                remote = response["Body"].read()
                if hashlib.sha256(local).digest() != hashlib.sha256(remote).digest():
                    raise CloudError("Una muestra remota no coincide con el archivo local")

    def upload_manifest(self, key: str, manifest: dict[str, Any]) -> None:
        body = json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8")
        self.client.put_object(
            Bucket=self.config.bucket,
            Key=key + ".json",
            Body=body,
            ContentType="application/json",
            ServerSideEncryption="AES256",
        )
