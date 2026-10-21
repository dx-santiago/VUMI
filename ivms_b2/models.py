from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Callable


@dataclass(frozen=True)
class Segment:
    start: datetime
    end: datetime
    playback_uri: str
    size: int | None = None
    source_start: datetime | None = None
    source_end: datetime | None = None


@dataclass(frozen=True)
class DayWindow:
    day: date
    start: datetime
    end: datetime


@dataclass
class DeviceConfig:
    host: str
    http_port: int
    rtsp_port: int
    username: str
    utc_offset: str = "-05:00"
    verify_tls: bool = False


@dataclass(frozen=True)
class CameraConfig:
    channel: int
    camera_name: str
    source_id: str = ""
    source_name: str = ""
    storage_source: str = ""

    @property
    def display_name(self) -> str:
        return f"{self.source_name} / {self.camera_name}" if self.source_name else self.camera_name


@dataclass
class CloudConfig:
    endpoint: str
    bucket: str
    prefix: str = "ivms"
    part_size_mib: int = 64
    concurrency: int = 4


@dataclass
class JobConfig:
    device: DeviceConfig
    camera: CameraConfig
    cloud: CloudConfig
    start_date: date
    end_date: date
    start_time: time
    end_time: time
    spool_dir: Path
    business_days_only: bool = True

    def windows(self) -> list[DayWindow]:
        if self.end_date < self.start_date:
            raise ValueError("La fecha final no puede ser anterior a la inicial")
        if self.end_time <= self.start_time:
            raise ValueError("La hora final debe ser posterior a la inicial")
        tz = parse_utc_offset(self.device.utc_offset)
        result: list[DayWindow] = []
        day = self.start_date
        while day <= self.end_date:
            # weekday(): lunes=0 ... domingo=6. Por defecto solo se procesan
            # días de semana laboral; los festivos no se excluyen.
            if not self.business_days_only or day.weekday() < 5:
                result.append(
                    DayWindow(
                        day=day,
                        start=datetime.combine(day, self.start_time, tz),
                        end=datetime.combine(day, self.end_time, tz),
                    )
                )
            day += timedelta(days=1)
        return result


@dataclass
class DayState:
    status: str = "pendiente"
    local_path: str = ""
    object_key: str = ""
    size: int = 0
    sha256: str = ""
    expected_size: int = 0
    missing_intervals: list[dict[str, str]] = field(default_factory=list)
    segments: list[dict[str, Any]] = field(default_factory=list)
    multipart: dict[str, Any] = field(default_factory=dict)
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


ProgressCallback = Callable[[str, str, float | None], None]
BatchProgressCallback = Callable[[CameraConfig, str, str, float | None], None]


def parse_utc_offset(value: str) -> timezone:
    raw = value.strip()
    if len(raw) != 6 or raw[0] not in "+-" or raw[3] != ":":
        raise ValueError("El desfase UTC debe tener formato -05:00 o +01:00")
    hours, minutes = int(raw[1:3]), int(raw[4:6])
    if hours > 14 or minutes > 59:
        raise ValueError("Desfase UTC fuera de rango")
    delta = timedelta(hours=hours, minutes=minutes)
    if raw[0] == "-":
        delta = -delta
    return timezone(delta)
