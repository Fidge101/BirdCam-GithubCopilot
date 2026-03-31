"""Camera stream wrapper around OpenCV RTSP capture.

This module keeps RTSP connection handling in one place so the live viewer and
background scheduler can share the same camera access logic.
"""

from __future__ import annotations

import logging
import threading
import time
from urllib.parse import urlsplit, urlunsplit

import cv2
import numpy as np


LOGGER = logging.getLogger(__name__)


def _mask_rtsp_credentials(rtsp_url: str) -> str:
    """Return an RTSP URL with password redacted for safe logging.

    Connection logs are useful for diagnostics, but credentials should never be
    exposed in plaintext to terminal output or service logs.
    """

    parts = urlsplit(rtsp_url)
    if not parts.netloc or "@" not in parts.netloc:
        return rtsp_url

    userinfo, hostinfo = parts.netloc.rsplit("@", 1)
    username = userinfo.split(":", 1)[0] if userinfo else ""
    safe_userinfo = f"{username}:***" if username else "***"
    safe_netloc = f"{safe_userinfo}@{hostinfo}"
    return urlunsplit((parts.scheme, safe_netloc, parts.path, parts.query, parts.fragment))


class CameraStream:
    """Manage an RTSP connection using OpenCV VideoCapture.

    OpenCV handles RTSP buffering and frame decoding directly, which keeps the
    Raspberry Pi dependency footprint small while still supporting reconnects.
    """

    def __init__(self, rtsp_url: str, blank_frame_reconnect_threshold: int = 3) -> None:
        """Store the RTSP URL and prepare a reusable VideoCapture instance.

        The instance keeps a lock because the scheduler and viewer may read from
        the same stream concurrently when the application runs in combined mode.
        """

        self.rtsp_url = rtsp_url
        self._safe_rtsp_url = _mask_rtsp_credentials(rtsp_url)
        self.capture = cv2.VideoCapture()
        self._lock = threading.Lock()
        self.blank_frame_reconnect_threshold = max(1, int(blank_frame_reconnect_threshold))
        self._consecutive_blank_frames = 0
        self._previous_timestamp_sample: np.ndarray | None = None
        self._consecutive_stale_timestamp_checks = 0
        self._timestamp_region_difference_threshold = 2.2
        self._stale_timestamp_reconnect_checks = 4
        self._health_check_interval_seconds = 1.0
        self._last_health_check_monotonic = 0.0
        self._reconnect_cooldown_seconds = 8.0
        self._last_reconnect_attempt_monotonic = 0.0

    def _reset_health_tracking(self) -> None:
        """Reset counters used to detect unhealthy stream output."""

        self._consecutive_blank_frames = 0
        self._consecutive_stale_timestamp_checks = 0
        self._previous_timestamp_sample = None
        self._last_health_check_monotonic = 0.0

    def _can_attempt_reconnect(self, now_monotonic: float) -> bool:
        """Return True when reconnect cooldown has elapsed."""

        return (now_monotonic - self._last_reconnect_attempt_monotonic) >= self._reconnect_cooldown_seconds

    def _mark_reconnect_attempt(self, now_monotonic: float) -> None:
        """Record when reconnect was attempted to enforce cooldown."""

        self._last_reconnect_attempt_monotonic = now_monotonic

    def _build_timestamp_sample(self, frame: np.ndarray) -> np.ndarray:
        """Build a compact grayscale sample from the timestamp overlay area."""

        height, width = frame.shape[:2]
        roi_height = max(14, min(height, int(height * 0.18)))
        roi_width = max(40, min(width, int(width * 0.35)))
        roi = frame[0:roi_height, 0:roi_width]
        grayscale = cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY)
        return cv2.resize(grayscale, (48, 16), interpolation=cv2.INTER_AREA)

    def _timestamp_region_advanced(self, frame: np.ndarray) -> bool:
        """Return True if the timestamp overlay region appears to have changed."""

        if frame.size == 0:
            self._previous_timestamp_sample = None
            return False

        sample = self._build_timestamp_sample(frame)
        if self._previous_timestamp_sample is None:
            self._previous_timestamp_sample = sample
            return True

        difference = float(
            np.mean(
                np.abs(sample.astype(np.int16) - self._previous_timestamp_sample.astype(np.int16))
            )
        )
        self._previous_timestamp_sample = sample
        return difference > self._timestamp_region_difference_threshold

    def connect(self) -> bool:
        """Open the RTSP stream and report whether the connection succeeded.

        A dedicated connect step keeps startup failures explicit instead of
        letting the first frame read fail deeper inside the application.
        """

        with self._lock:
            if self.capture.isOpened():
                LOGGER.debug("RTSP stream is already open")
                return True

            LOGGER.info("Connecting to camera stream at %s", self._safe_rtsp_url)
            self.capture.open(self.rtsp_url)
            if not self.capture.isOpened():
                LOGGER.error("Failed to open RTSP stream: %s", self._safe_rtsp_url)
                return False

            self._reset_health_tracking()
            LOGGER.info("Connected to camera stream")
            return True

    def reconnect(self) -> bool:
        """Retry opening the RTSP stream up to three times with backoff.

        WiFi cameras often drop idle or weak connections, so bounded retries
        keep the app resilient without blocking forever.
        """

        for attempt in range(1, 4):
            LOGGER.warning("Reconnecting to camera stream (attempt %s/3)", attempt)
            self.release()
            if self.connect():
                return True
            if attempt < 3:
                time.sleep(5)

        LOGGER.error("Unable to reconnect to camera stream after 3 attempts")
        return False

    def read_frame(self) -> np.ndarray | None:
        """Return the latest decoded frame or None if the read fails.

        The method attempts a reconnect before giving up so temporary network
        interruptions do not immediately stop the viewer or capture thread.
        """

        reconnect_reason: str | None = None
        now_monotonic = time.monotonic()

        with self._lock:
            if not self.capture.isOpened():
                LOGGER.warning("RTSP stream is not open when attempting to read")
                self._consecutive_blank_frames += 1
                reconnect_reason = "stream closed"
            else:
                success, frame = self.capture.read()
                if success and frame is not None and frame.size > 0:
                    self._consecutive_blank_frames = 0

                    if (now_monotonic - self._last_health_check_monotonic) >= self._health_check_interval_seconds:
                        self._last_health_check_monotonic = now_monotonic
                        if self._timestamp_region_advanced(frame):
                            self._consecutive_stale_timestamp_checks = 0
                        else:
                            self._consecutive_stale_timestamp_checks += 1
                            LOGGER.warning(
                                "Timestamp region unchanged (%s/%s before reconnect)",
                                self._consecutive_stale_timestamp_checks,
                                self._stale_timestamp_reconnect_checks,
                            )
                            if self._consecutive_stale_timestamp_checks >= self._stale_timestamp_reconnect_checks:
                                reconnect_reason = "timestamp stalled"

                    if reconnect_reason is None:
                        return frame
                else:
                    self._consecutive_stale_timestamp_checks = 0
                    self._previous_timestamp_sample = None
                    self._consecutive_blank_frames += 1
                    LOGGER.warning(
                        "Failed to read frame (%s/%s before reconnect)",
                        self._consecutive_blank_frames,
                        self.blank_frame_reconnect_threshold,
                    )
                    reconnect_reason = "read failure"

        if reconnect_reason == "read failure" and self._consecutive_blank_frames < self.blank_frame_reconnect_threshold:
            return None

        if reconnect_reason is None:
            return None

        if not self._can_attempt_reconnect(now_monotonic):
            LOGGER.debug("Reconnect skipped due to cooldown (%s)", reconnect_reason)
            return None

        LOGGER.warning(
            "Forcing reconnect (%s)",
            reconnect_reason,
        )
        self._mark_reconnect_attempt(now_monotonic)
        if self.reconnect():
            with self._lock:
                success, frame = self.capture.read()
                if success and frame is not None and frame.size > 0:
                    self._consecutive_blank_frames = 0
                    self._consecutive_stale_timestamp_checks = 0
                    self._last_health_check_monotonic = now_monotonic
                    self._timestamp_region_advanced(frame)
                    return frame
                else:
                    self._consecutive_stale_timestamp_checks = 0
                    self._previous_timestamp_sample = None
                LOGGER.error("Frame read failed after reconnect")

        return None

    def release(self) -> None:
        """Close the RTSP stream if it is open.

        Explicit cleanup prevents resource leaks and releases camera handles
        cleanly during shutdown or reconnect attempts.
        """

        with self._lock:
            if self.capture.isOpened():
                self.capture.release()
                self._reset_health_tracking()
                LOGGER.info("Camera stream released")