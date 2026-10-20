from __future__ import annotations

import re
import uuid
from dataclasses import replace
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlparse
from xml.etree import ElementTree as ET

import requests
from requests.auth import HTTPDigestAuth

from .models import CameraConfig, DeviceConfig, Segment, parse_utc_offset


NS = "http://www.hikvision.com/ver20/XMLSchema"


class IsapiError(RuntimeError):
    pass


class IsapiClient:
    def __init__(self, config: DeviceConfig, password: str, timeout: int = 30):
        self.config = config
        self.password = password
        scheme = "https" if config.verify_tls else "http"
        self.base_url = f"{scheme}://{config.host}:{config.http_port}"
        self.timeout = timeout
        self.session = requests.Session()
        self.session.auth = HTTPDigestAuth(config.username, password)
        self.session.verify = config.verify_tls

    def _request(self, method: str, path: str, **kwargs) -> requests.Response:
        try:
            response = self.session.request(
                method,
                self.base_url + path,
                timeout=self.timeout,
                **kwargs,
            )
        except requests.RequestException as exc:
            raise IsapiError(f"No fue posible conectar con el grabador: {exc}") from exc
        if response.status_code == 401:
            raise IsapiError("Usuario o contraseña del grabador incorrectos")
        if response.status_code >= 400:
            raise IsapiError(f"ISAPI respondió HTTP {response.status_code} en {path}")
        return response

    def test_connection(self) -> dict[str, str]:
        device = self._request("GET", "/ISAPI/System/deviceInfo").content
        result = parse_simple_xml(device, ("deviceName", "model", "serialNumber", "firmwareVersion"))
        try:
            clock = self._request("GET", "/ISAPI/System/time").content
            result.update(parse_simple_xml(clock, ("localTime", "timeZone")))
        except IsapiError:
            pass
        return result

    def list_cameras(self) -> list[CameraConfig]:
        cameras_by_channel: dict[int, CameraConfig] = {}
        errors: list[str] = []
        for path in ("/ISAPI/System/Video/inputs/channels", "/ISAPI/ContentMgmt/InputProxy/channels"):
            try:
                response = self._request("GET", path)
                for camera in parse_camera_response(response.content):
                    cameras_by_channel.setdefault(camera.channel, camera)
            except IsapiError as exc:
                errors.append(str(exc))
        cameras = sorted(cameras_by_channel.values(), key=lambda item: item.channel)
        if not cameras:
            detail = f" ({errors[-1]})" if errors else ""
            raise IsapiError(f"El grabador no informó canales de video{detail}; agréguelos manualmente")
        return cameras

    def search_recordings(self, start: datetime, end: datetime, channel: int) -> list[Segment]:
        position = 0
        page_size = 40
        search_id = str(uuid.uuid4())
        track_id = channel * 100 + 1
        all_segments: list[Segment] = []
        while True:
            payload = _search_xml(search_id, track_id, start, end, position, page_size)
            response = self._request(
                "POST",
                "/ISAPI/ContentMgmt/search",
                data=payload,
                headers={"Content-Type": "application/xml"},
            )
            page, more = parse_search_response(response.content)
            # Hikvision usa el sufijo Z en estos endpoints, pero este firmware
            # interpreta los campos como hora de pared del dispositivo. No se
            # debe convertir 07:00 Colombia a 12:00 UTC.
            device_tz = parse_utc_offset(self.config.utc_offset)
            page = [
                replace(
                    item,
                    start=item.start.replace(tzinfo=device_tz),
                    end=item.end.replace(tzinfo=device_tz),
                )
                for item in page
            ]
            all_segments.extend(page)
            if not more or not page:
                break
            position += len(page)
            if position > 100000:
                raise IsapiError("La búsqueda devolvió demasiados resultados")
        unique: dict[tuple[datetime, datetime, str], Segment] = {}
        for item in all_segments:
            unique[(item.start, item.end, item.playback_uri)] = item
        return sorted(unique.values(), key=lambda item: item.start)

    def open_download(self, playback_uri: str) -> requests.Response:
        """Abre una descarga acelerada por nombre usando el URI de búsqueda."""
        root = ET.Element(
            "downloadRequest",
            {"version": "1.0", "xmlns": "http://www.isapi.org/ver20/XMLSchema"},
        )
        ET.SubElement(root, "playbackURI").text = playback_uri
        body = ET.tostring(root, encoding="utf-8", xml_declaration=True)
        try:
            response = self.session.request(
                "GET",
                self.base_url + "/ISAPI/ContentMgmt/download",
                data=body,
                headers={"Content-Type": "application/xml"},
                stream=True,
                timeout=(10, 60),
            )
        except requests.RequestException as exc:
            raise IsapiError(f"Falló la descarga HTTP desde el grabador: {exc}") from exc
        if response.status_code == 401:
            response.close()
            raise IsapiError("Usuario o contraseña del grabador incorrectos")
        if response.status_code >= 400:
            status = response.status_code
            response.close()
            raise IsapiError(f"El grabador rechazó la descarga HTTP ({status})")
        content_type = response.headers.get("Content-Type", "").lower()
        if "xml" in content_type:
            detail = response.text[:300]
            response.close()
            raise IsapiError(f"El grabador devolvió un error XML al descargar: {detail}")
        return response


def _search_xml(
    search_id: str,
    track_id: int,
    start: datetime,
    end: datetime,
    position: int,
    max_results: int,
) -> bytes:
    def device_wall_time(value: datetime) -> str:
        return value.strftime("%Y-%m-%dT%H:%M:%SZ")

    root = ET.Element("CMSearchDescription", {"xmlns": NS})
    ET.SubElement(root, "searchID").text = search_id
    tracks = ET.SubElement(root, "trackList")
    ET.SubElement(tracks, "trackID").text = str(track_id)
    spans = ET.SubElement(root, "timeSpanList")
    span = ET.SubElement(spans, "timeSpan")
    ET.SubElement(span, "startTime").text = device_wall_time(start)
    ET.SubElement(span, "endTime").text = device_wall_time(end)
    ET.SubElement(root, "maxResults").text = str(max_results)
    ET.SubElement(root, "searchResultPostion").text = str(position)
    metadata = ET.SubElement(root, "metadataList")
    ET.SubElement(metadata, "metadataDescriptor").text = "//recordType.meta.std-cgi.com"
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _child_text(element: ET.Element, name: str) -> str:
    for child in element.iter():
        if _local_name(child.tag) == name and child.text:
            return child.text.strip()
    return ""


def _direct_child_text(element: ET.Element, name: str) -> str:
    for child in element:
        if _local_name(child.tag) == name and child.text:
            return child.text.strip()
    return ""


def parse_simple_xml(data: bytes, fields: tuple[str, ...]) -> dict[str, str]:
    try:
        root = ET.fromstring(data)
    except ET.ParseError as exc:
        raise IsapiError("El grabador devolvió XML inválido") from exc
    return {field: _child_text(root, field) for field in fields if _child_text(root, field)}


def parse_camera_response(data: bytes) -> list[CameraConfig]:
    try:
        root = ET.fromstring(data)
    except ET.ParseError as exc:
        raise IsapiError("El grabador devolvió una lista de cámaras inválida") from exc
    cameras: list[CameraConfig] = []
    seen: set[int] = set()
    for element in root.iter():
        if _local_name(element.tag) not in {"VideoInputChannel", "InputProxyChannel"}:
            continue
        raw_id = _direct_child_text(element, "id")
        if not raw_id:
            continue
        try:
            channel = int(raw_id)
        except ValueError:
            continue
        if channel <= 0 or channel in seen:
            continue
        name = _direct_child_text(element, "name") or f"Camera_{channel}"
        cameras.append(CameraConfig(channel, name.strip()))
        seen.add(channel)
    return sorted(cameras, key=lambda item: item.channel)


def parse_isapi_datetime(value: str) -> datetime:
    normalized = value.strip().replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise IsapiError(f"Fecha ISAPI inválida: {value}") from exc


def _size_from_uri(uri: str) -> int | None:
    values = parse_qs(urlparse(uri).query).get("size")
    if not values:
        return None
    raw = re.sub(r"\D", "", values[0])
    return int(raw) if raw else None


def recording_name(uri: str) -> str:
    values = parse_qs(urlparse(uri).query).get("name")
    return values[0] if values else ""


def parse_search_response(data: bytes) -> tuple[list[Segment], bool]:
    try:
        root = ET.fromstring(data)
    except ET.ParseError as exc:
        raise IsapiError("La búsqueda ISAPI devolvió XML inválido") from exc
    status = _child_text(root, "responseStatusStrg").upper()
    if status and status not in {"OK", "MORE"}:
        raise IsapiError(f"La búsqueda ISAPI falló: {status}")
    result: list[Segment] = []
    for element in root.iter():
        if _local_name(element.tag) != "searchMatchItem":
            continue
        uri = _child_text(element, "playbackURI")
        start = _child_text(element, "startTime")
        end = _child_text(element, "endTime")
        if uri and start and end:
            result.append(
                Segment(
                    start=parse_isapi_datetime(start),
                    end=parse_isapi_datetime(end),
                    playback_uri=uri,
                    size=_size_from_uri(uri),
                )
            )
    return result, status == "MORE"
