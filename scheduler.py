"""Background frame capture scheduler.

This module runs periodic JPEG capture in a thread so frame storage can happen
at the same time as an interactive live view on the Raspberry Pi.
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime, timedelta
from pathlib import Path

import cv2

from camera import CameraStream
from config import AppConfig
from timelapse import generate_daily_timelapse_export


LOGGER = logging.getLogger(__name__)


def _delete_oldest_frames(frame_dir: Path, max_frames: int) -> None:
    """Delete oldest JPEG frames when the retention limit is exceeded.

    Filename timestamps sort chronologically, so pruning by sorted name keeps
    storage bounded without needing a separate index.
    """

    frames = sorted(frame_dir.glob("*.jpg"))
    overflow = len(frames) - max_frames
    if overflow <= 0:
        return

    for frame_path in frames[:overflow]:
        frame_path.unlink(missing_ok=True)
        LOGGER.info("Deleted old frame %s to enforce retention limit", frame_path)


def capture_single_frame(camera_stream: CameraStream, config: AppConfig) -> Path | None:
    """Capture one frame immediately, enforce retention, and return the saved path.

    This helper is reused by both the scheduler loop and web API so manual and
    periodic captures follow the same naming, retention, and logging behavior.
    """

    frame = camera_stream.read_frame()
    if frame is None:
        LOGGER.warning("Capture skipped because no frame was available")
        return None

    timestamp = datetime.now()
    filename = timestamp.strftime("%Y%m%d_%H%M%S.jpg")
    output_path = config.frame_store_dir / filename

    if cv2.imwrite(str(output_path), frame):
        LOGGER.info("Captured frame at %s -> %s", timestamp.isoformat(), output_path)
        _delete_oldest_frames(config.frame_store_dir, config.max_frames)
        return output_path

    LOGGER.error("Failed to save frame to %s", output_path)
    return None


def _capture_loop(camera_stream: CameraStream, config: AppConfig, stop_event: threading.Event) -> None:
    """Capture frames at a fixed interval until the stop event is set.

    A dedicated loop function keeps the thread target simple and testable while
    consolidating retention and logging behavior in one place.
    """

    LOGGER.info(
        "Capture scheduler started with %s-second interval",
        config.capture_interval_seconds,
    )

    while not stop_event.is_set():
        capture_single_frame(camera_stream, config)

        stop_event.wait(config.capture_interval_seconds)

    LOGGER.info("Capture scheduler stopped")


def _daily_export_loop(config: AppConfig, stop_event: threading.Event) -> None:
    """Generate a dated timelapse export once per day at configured local time."""

    if not config.daily_export_enabled:
        LOGGER.info("Daily timelapse export scheduler is disabled")
        return

    export_hour, export_minute = (int(part) for part in config.daily_export_time.split(":", 1))
    last_run_date = None

    LOGGER.info(
        "Daily timelapse export scheduler started at %s (output dir: %s)",
        config.daily_export_time,
        config.daily_export_dir,
    )

    while not stop_event.is_set():
        now = datetime.now()
        if (now.hour, now.minute) >= (export_hour, export_minute) and last_run_date != now.date():
            target_date = now.date() - timedelta(days=1)
            try:
                output_path = generate_daily_timelapse_export(
                    config.frame_store_dir,
                    config.daily_export_dir,
                    target_date,
                    columns=10,
                )
                LOGGER.info("Daily timelapse export generated for %s -> %s", target_date.isoformat(), output_path)
            except ValueError as exc:
                LOGGER.warning("Daily export skipped for %s: %s", target_date.isoformat(), exc)
            except Exception:  # pragma: no cover - defensive guard
                LOGGER.exception("Daily export failed for %s", target_date.isoformat())
            last_run_date = now.date()

        stop_event.wait(30)

    LOGGER.info("Daily timelapse export scheduler stopped")


def run_scheduler(
    camera_stream: CameraStream,
    config: AppConfig,
    stop_event: threading.Event | None = None,
) -> threading.Thread:
    """Start periodic frame capture in a background thread and return it.

    Running the scheduler in a thread allows the Raspberry Pi to keep showing a
    live feed while continuing to store periodic snapshots.
    """

    if stop_event is None:
        stop_event = threading.Event()

    worker = threading.Thread(
        target=_capture_loop,
        args=(camera_stream, config, stop_event),
        name="frame-capture-scheduler",
        daemon=True,
    )
    worker.start()

    daily_worker = threading.Thread(
        target=_daily_export_loop,
        args=(config, stop_event),
        name="daily-timelapse-export-scheduler",
        daemon=True,
    )
    daily_worker.start()

    return worker