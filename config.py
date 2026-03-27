"""Application configuration loaded from environment variables.

This module centralizes configuration so credentials and runtime settings are
loaded from a .env file instead of being hardcoded across modules.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
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

    load_dotenv(dotenv_path=env_path)

    camera_ip = _require_env("CAMERA_IP")
    camera_user = _require_env("CAMERA_USER")
    camera_pass = _require_env("CAMERA_PASS")
    capture_interval_seconds = int(_require_env("CAPTURE_INTERVAL_SECONDS"))
    timelapse_output_path = Path(_require_env("TIMELAPSE_OUTPUT_PATH"))
    frame_store_dir = Path(_require_env("FRAME_STORE_DIR"))
    max_frames = int(_require_env("MAX_FRAMES"))

    if capture_interval_seconds <= 0:
        raise ValueError("CAPTURE_INTERVAL_SECONDS must be greater than zero")
    if max_frames <= 0:
        raise ValueError("MAX_FRAMES must be greater than zero")

    frame_store_dir.mkdir(parents=True, exist_ok=True)
    timelapse_output_path.parent.mkdir(parents=True, exist_ok=True)

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
    )