from __future__ import annotations

import uuid
from typing import Any

from .config import camera_entries


def load_profiles(data: dict[str, Any]) -> list[dict[str, Any]]:
    raw_profiles = data.get("dvr_profiles")
    if isinstance(raw_profiles, list) and raw_profiles:
        profiles = [_normalize_profile(item, index) for index, item in enumerate(raw_profiles, 1) if isinstance(item, dict)]
        if profiles:
            return profiles

    legacy_host = str(data.get("host") or "").strip()
    legacy_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"vumi-dvr:{legacy_host or 'principal'}"))
    return [
        _normalize_profile(
            {
                "id": legacy_id,
                "name": "DVR principal",
                "host": legacy_host,
                "http_port": data.get("http_port", "80"),
                "rtsp_port": data.get("rtsp_port", "554"),
                "username": data.get("nvr_user", "admin"),
                "utc_offset": data.get("utc_offset", "-05:00"),
                "cameras": camera_entries(data),
                "use_namespace": False,
                "storage_name": "",
            },
            1,
        )
    ]


def new_profile(name: str) -> dict[str, Any]:
    return _normalize_profile({"id": str(uuid.uuid4()), "name": name}, 1)


def public_profiles(profiles: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [_normalize_profile(profile, index) for index, profile in enumerate(profiles, 1)]


def _normalize_profile(value: dict[str, Any], index: int) -> dict[str, Any]:
    cameras = []
    for raw in value.get("cameras") or []:
        if not isinstance(raw, dict):
            continue
        try:
            channel = int(raw.get("channel"))
        except (TypeError, ValueError):
            continue
        if channel <= 0:
            continue
        cameras.append(
            {
                "channel": channel,
                "camera_name": str(raw.get("camera_name") or f"Camera_{channel}"),
                "selected": bool(raw.get("selected", False)),
            }
        )
    return {
        "id": str(value.get("id") or uuid.uuid4()),
        "name": str(value.get("name") or f"DVR {index}").strip() or f"DVR {index}",
        "host": str(value.get("host") or "").strip(),
        "http_port": str(value.get("http_port") or "80"),
        "rtsp_port": str(value.get("rtsp_port") or "554"),
        "username": str(value.get("username") or value.get("nvr_user") or "admin").strip(),
        "utc_offset": str(value.get("utc_offset") or "-05:00").strip(),
        "use_namespace": bool(value.get("use_namespace", True)),
        "storage_name": str(value.get("storage_name") or "").strip(),
        "cameras": cameras,
    }
