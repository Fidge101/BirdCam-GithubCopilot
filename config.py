"""Application configuration loaded from environment variables.

This module centralizes configuration so credentials and runtime settings are
loaded from a .env file instead of being hardcoded across modules.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv


LOGGER = logging.getLogger(__name__)


@dataclass
class AppConfig:
    """Store validated runtime settings for camera access and frame capture.

    The dataclass keeps configuration immutable after startup so every module
    reads the same values and accidental runtime mutation is avoided.
    """

    camera_ip: str
    camera_user: str
    camera_pass: str
    capture_interval_seconds: int
    timelapse_output_path: Path
    frame_store_dir: Path
    max_frames: int
    rtsp_url: str
    stream_quality: int
    port: int
    log_file_path: Path
    env_path: Path
    daily_export_enabled: bool
    daily_export_time: str
    daily_export_dir: Path
    timelapse_width: int
    timelapse_height: int


def _parse_bool(value: str | None, default: bool) -> bool:
    """Parse common environment boolean strings with a default fallback."""

    if value is None:
        return default
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "y", "on"}:
        return True
    if normalized in {"0", "false", "no", "n", "off"}:
        return False
    raise ValueError(f"Invalid boolean value: {value}")


def _require_env(name: str) -> str:
    """Return a required environment variable or raise a clear error.

    Explicit validation makes startup failures immediate and easier to diagnose
    on a Raspberry Pi where services may be launched unattended.
    """

    value = os.getenv(name)
    if value is None or not value.strip():
        raise ValueError(f"Missing required environment variable: {name}")
    return value.strip()


def load_config(env_path: str | Path = ".env") -> AppConfig:
    """Load and validate configuration from a .env file.

    The dotenv file allows camera credentials and file paths to be configured
    per device without editing source code.
    """

    env_path = Path(env_path)
    env_dir = env_path.expanduser().resolve().parent
    load_dotenv(dotenv_path=env_path)

    camera_ip = _require_env("CAMERA_IP")
    camera_user = _require_env("CAMERA_USER")
    camera_pass = _require_env("CAMERA_PASS")
    capture_interval_seconds = int(_require_env("CAPTURE_INTERVAL_SECONDS"))
    timelapse_output_path_raw = Path(_require_env("TIMELAPSE_OUTPUT_PATH"))
    frame_store_dir_raw = Path(_require_env("FRAME_STORE_DIR"))
    max_frames = int(_require_env("MAX_FRAMES"))
    stream_quality = int(os.getenv("STREAM_QUALITY", "80"))
    port = int(os.getenv("PORT", "5000"))
    log_file_path_raw = Path(os.getenv("LOG_FILE_PATH", "./birdcam.log"))
    daily_export_enabled = _parse_bool(os.getenv("DAILY_EXPORT_ENABLED"), True)
    daily_export_time = os.getenv("DAILY_EXPORT_TIME", "00:10").strip()
    daily_export_dir_raw = Path(os.getenv("DAILY_EXPORT_DIR", "./output/daily"))
    timelapse_width = int(os.getenv("TIMELAPSE_WIDTH", "320"))
    timelapse_height = int(os.getenv("TIMELAPSE_HEIGHT", "180"))

    timelapse_output_path = (
        timelapse_output_path_raw
        if timelapse_output_path_raw.is_absolute()
        else (env_dir / timelapse_output_path_raw)
    ).resolve()
    frame_store_dir = (
        frame_store_dir_raw if frame_store_dir_raw.is_absolute() else (env_dir / frame_store_dir_raw)
    ).resolve()
    log_file_path = (
        log_file_path_raw if log_file_path_raw.is_absolute() else (env_dir / log_file_path_raw)
    ).resolve()
    daily_export_dir = (
        daily_export_dir_raw if daily_export_dir_raw.is_absolute() else (env_dir / daily_export_dir_raw)
    ).resolve()

    if capture_interval_seconds <= 0:
        raise ValueError("CAPTURE_INTERVAL_SECONDS must be greater than zero")
    if max_frames <= 0:
        raise ValueError("MAX_FRAMES must be greater than zero")
    if not 0 <= stream_quality <= 100:
        raise ValueError("STREAM_QUALITY must be between 0 and 100")
    if not 1 <= port <= 65535:
        raise ValueError("PORT must be between 1 and 65535")
    if timelapse_width < 64 or timelapse_width > 3840:
        raise ValueError("TIMELAPSE_WIDTH must be between 64 and 3840")
    if timelapse_height < 64 or timelapse_height > 2160:
        raise ValueError("TIMELAPSE_HEIGHT must be between 64 and 2160")
    try:
        datetime.strptime(daily_export_time, "%H:%M")
    except ValueError as exc:
        raise ValueError("DAILY_EXPORT_TIME must use HH:MM 24-hour format") from exc

    frame_store_dir.mkdir(parents=True, exist_ok=True)
    timelapse_output_path.parent.mkdir(parents=True, exist_ok=True)
    log_file_path.parent.mkdir(parents=True, exist_ok=True)
    daily_export_dir.mkdir(parents=True, exist_ok=True)

    # Tapo C120 exposes /stream1 as the primary HD RTSP feed; /stream2 is the
    # lower-resolution sub-stream intended for lighter-bandwidth clients.
    rtsp_url = f"rtsp://{camera_user}:{camera_pass}@{camera_ip}/stream1"

    LOGGER.debug("Configuration loaded for camera at %s", camera_ip)

    return AppConfig(
        camera_ip=camera_ip,
        camera_user=camera_user,
        camera_pass=camera_pass,
        capture_interval_seconds=capture_interval_seconds,
        timelapse_output_path=timelapse_output_path,
        frame_store_dir=frame_store_dir,
        max_frames=max_frames,
        rtsp_url=rtsp_url,
        stream_quality=stream_quality,
        port=port,
        log_file_path=log_file_path,
        env_path=env_path.expanduser().resolve(),
        daily_export_enabled=daily_export_enabled,
        daily_export_time=daily_export_time,
        daily_export_dir=daily_export_dir,
        timelapse_width=timelapse_width,
        timelapse_height=timelapse_height,
    )