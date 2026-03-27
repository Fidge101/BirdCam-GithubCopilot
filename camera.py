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

    def __init__(self, rtsp_url: str) -> None:
        """Store the RTSP URL and prepare a reusable VideoCapture instance.

        The instance keeps a lock because the scheduler and viewer may read from
        the same stream concurrently when the application runs in combined mode.
        """

        self.rtsp_url = rtsp_url
        self._safe_rtsp_url = _mask_rtsp_credentials(rtsp_url)
        self.capture = cv2.VideoCapture()
        self._lock = threading.Lock()

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
            else:
                success, frame = self.capture.read()
                if success and frame is not None:
                    return frame
                LOGGER.error("Failed to read frame from camera stream")

        if self.reconnect():
            with self._lock:
                success, frame = self.capture.read()
                if success and frame is not None:
                    return frame
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
                LOGGER.info("Camera stream released")