from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

try:
    import keyring
except ImportError:  # pragma: no cover - mensaje tratado por la GUI
    keyring = None


APP_NAME = "IVMSBackblazeDownloader"


def app_dir() -> Path:
    root = Path(os.environ.get("APPDATA", Path.home()))
    return root / APP_NAME


def config_path() -> Path:
    return app_dir() / "config.json"


def load_config() -> dict[str, Any]:
    path = config_path()
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def camera_entries(data: dict[str, Any]) -> list[dict[str, Any]]:
    """Normaliza la lista de cámaras y migra el formato de una sola cámara."""
    cameras = data.get("cameras")
    if isinstance(cameras, list):
        return cameras
    if data.get("channel") or data.get("camera"):
        return [
            {
                "channel": data.get("channel", 1),
                "camera_name": data.get("camera", "Camera_1"),
                "selected": True,
            }
        ]
    return []


def save_config(data: dict[str, Any]) -> None:
    app_dir().mkdir(parents=True, exist_ok=True)
    safe = dict(data)
    for key in ("password", "application_key", "key_id"):
        safe.pop(key, None)
    atomic_write_json(config_path(), safe)


def atomic_write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, path)


def _require_keyring() -> None:
    if keyring is None:
        raise RuntimeError("Falta instalar 'keyring' para guardar credenciales en Windows")


def save_secret(name: str, value: dict[str, str]) -> None:
    _require_keyring()
    keyring.set_password(APP_NAME, name, json.dumps(value))


def load_secret(name: str) -> dict[str, str]:
    if keyring is None:
        return {}
    raw = keyring.get_password(APP_NAME, name)
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {}


def delete_secret(name: str) -> None:
    if keyring is None:
        return
    try:
        keyring.delete_password(APP_NAME, name)
    except keyring.errors.PasswordDeleteError:
        pass
