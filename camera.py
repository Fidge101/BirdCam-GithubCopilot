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
        self._previous_frame_sample: np.ndarray | None = None
        self._previous_timestamp_sample: np.ndarray | None = None
        self._consecutive_frozen_frames = 0
        self._black_frame_brightness_threshold = 8.0
        self._frozen_frame_threshold = 15
        self._frozen_frame_difference_threshold = 1.0
        self._timestamp_region_difference_threshold = 2.5

    def _reset_health_tracking(self) -> None:
        """Reset counters used to detect unhealthy stream output."""

        self._consecutive_blank_frames = 0
        self._consecutive_frozen_frames = 0
        self._previous_frame_sample = None
        self._previous_timestamp_sample = None

    def _build_frame_sample(self, frame: np.ndarray) -> np.ndarray:
        """Create a small grayscale sample for inexpensive health checks."""

        grayscale = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        return cv2.resize(grayscale, (32, 18), interpolation=cv2.INTER_AREA)

    def _build_timestamp_sample(self, frame: np.ndarray) -> np.ndarray:
        """Create a grayscale sample from the top-left timestamp overlay region."""

        height, width = frame.shape[:2]
        roi_height = max(16, min(height, int(height * 0.22)))
        roi_width = max(48, min(width, int(width * 0.45)))
        roi = frame[0:roi_height, 0:roi_width]
        grayscale = cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY)
        return cv2.resize(grayscale, (48, 16), interpolation=cv2.INTER_AREA)

    def _frame_is_healthy(self, frame: np.ndarray | None) -> tuple[bool, str | None]:
        """Return whether a frame looks usable for display and capture."""

        if frame is None or frame.size == 0:
            self._consecutive_frozen_frames = 0
            return False, "empty frame"

        sample = self._build_frame_sample(frame)
        timestamp_sample = self._build_timestamp_sample(frame)
        mean_brightness = float(sample.mean())
        if mean_brightness <= self._black_frame_brightness_threshold:
            self._consecutive_frozen_frames = 0
            self._previous_frame_sample = sample
            self._previous_timestamp_sample = timestamp_sample
            return False, f"black frame (mean brightness {mean_brightness:.1f})"

        if self._previous_frame_sample is not None:
            frame_difference = float(
                np.mean(
                    np.abs(sample.astype(np.int16) - self._previous_frame_sample.astype(np.int16))
                )
            )
            timestamp_difference = float("inf")
            if self._previous_timestamp_sample is not None:
                timestamp_difference = float(
                    np.mean(
                        np.abs(
                            timestamp_sample.astype(np.int16)
                            - self._previous_timestamp_sample.astype(np.int16)
                        )
                    )
                )

            looks_frozen = frame_difference <= self._frozen_frame_difference_threshold
            timestamp_is_advancing = timestamp_difference > self._timestamp_region_difference_threshold

            if looks_frozen and not timestamp_is_advancing:
                self._consecutive_frozen_frames += 1
                self._previous_frame_sample = sample
                self._previous_timestamp_sample = timestamp_sample
                if self._consecutive_frozen_frames >= self._frozen_frame_threshold:
                    return False, (
                        "frozen frame sequence "
                        f"({self._consecutive_frozen_frames} frames, "
                        f"frame diff {frame_difference:.2f}, "
                        f"timestamp diff {timestamp_difference:.2f})"
                    )
            else:
                self._consecutive_frozen_frames = 0
        else:
            self._consecutive_frozen_frames = 0

        self._previous_frame_sample = sample
        self._previous_timestamp_sample = timestamp_sample
        return True, None

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

        with self._lock:
            if not self.capture.isOpened():
                LOGGER.warning("RTSP stream is not open when attempting to read")
                self._consecutive_blank_frames += 1
            else:
                success, frame = self.capture.read()
                if success and frame is not None:
                    healthy, reason = self._frame_is_healthy(frame)
                    if healthy:
                        self._consecutive_blank_frames = 0
                        return frame
                    self._consecutive_blank_frames += 1
                    LOGGER.warning(
                        "Ignoring unhealthy frame (%s/%s before reconnect): %s",
                        self._consecutive_blank_frames,
                        self.blank_frame_reconnect_threshold,
                        reason,
                    )
                else:
                    self._consecutive_frozen_frames = 0
                    self._previous_frame_sample = None
                    self._previous_timestamp_sample = None
                    self._consecutive_blank_frames += 1
                    LOGGER.warning(
                        "Failed to read frame (%s/%s before reconnect)",
                        self._consecutive_blank_frames,
                        self.blank_frame_reconnect_threshold,
                    )

        if self._consecutive_blank_frames < self.blank_frame_reconnect_threshold:
            return None

        LOGGER.warning(
            "Reached blank frame reconnect threshold (%s), forcing reconnect",
            self.blank_frame_reconnect_threshold,
        )
        if self.reconnect():
            with self._lock:
                success, frame = self.capture.read()
                if success and frame is not None:
                    healthy, reason = self._frame_is_healthy(frame)
                    if healthy:
                        self._consecutive_blank_frames = 0
                        return frame
                    LOGGER.error("Frame read after reconnect was unhealthy: %s", reason)
                else:
                    self._consecutive_frozen_frames = 0
                    self._previous_frame_sample = None
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