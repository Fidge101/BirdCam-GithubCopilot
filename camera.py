"""Camera stream wrapper around OpenCV RTSP capture.

This module keeps RTSP connection handling in one place so the live viewer and
background scheduler can share the same camera access logic.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from urllib.parse import urlsplit, urlunsplit

import cv2


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
        self._blank_frame_reconnect_threshold = blank_frame_reconnect_threshold
        self._consecutive_blank_frames = 0

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
            # Force RTSP over TCP for reliable delivery on WiFi.  The OpenCV
            # default is UDP which drops packets frequently on wireless networks.
            os.environ.setdefault("OPENCV_FFMPEG_CAPTURE_OPTIONS", "rtsp_transport;tcp")
            self.capture = cv2.VideoCapture(self.rtsp_url, cv2.CAP_FFMPEG)
            # Minimise internal buffering so frames are always fresh.
            self.capture.set(cv2.CAP_PROP_BUFFERSIZE, 1)
            if not self.capture.isOpened():
                LOGGER.error("Failed to open RTSP stream: %s", self._safe_rtsp_url)
                return False

            self._consecutive_blank_frames = 0
            LOGGER.info("Connected to camera stream")
            return True

    def reconnect(self) -> bool:
        """Retry opening the RTSP stream up to three times with backoff.

        WiFi cameras often drop idle or weak connections, so bounded retries
        keep the app resilient without blocking forever.
        """

        self._consecutive_blank_frames = 0
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

        Consecutive blank or failed reads are counted against the configured
        threshold.  When the threshold is reached the stream is reconnected
        automatically so the application recovers without user intervention.
        """

        with self._lock:
            if not self.capture.isOpened():
                LOGGER.debug("RTSP stream is not open when attempting to read")
                self._consecutive_blank_frames += 1
                if self._consecutive_blank_frames >= self._blank_frame_reconnect_threshold:
                    LOGGER.warning(
                        "Stream has been closed for %s consecutive reads — attempting reconnect",
                        self._consecutive_blank_frames,
                    )
                    self._consecutive_blank_frames = 0
                    self.capture.release()
                    self.capture = cv2.VideoCapture(self.rtsp_url, cv2.CAP_FFMPEG)
                    self.capture.set(cv2.CAP_PROP_BUFFERSIZE, 1)
                    if self.capture.isOpened():
                        LOGGER.info("Automatic reconnect succeeded")
                return None

            success, frame = self.capture.read()
            if not success or frame is None or frame.size == 0:
                self._consecutive_blank_frames += 1
                if self._consecutive_blank_frames >= self._blank_frame_reconnect_threshold:
                    LOGGER.warning(
                        "%s consecutive blank/failed frames — attempting automatic reconnect",
                        self._consecutive_blank_frames,
                    )
                    self._consecutive_blank_frames = 0
                    self.capture.release()
                    self.capture = cv2.VideoCapture(self.rtsp_url, cv2.CAP_FFMPEG)
                    self.capture.set(cv2.CAP_PROP_BUFFERSIZE, 1)
                    if self.capture.isOpened():
                        LOGGER.info("Automatic reconnect succeeded")
                return None

            self._consecutive_blank_frames = 0
            return frame

    def release(self) -> None:
        """Close the RTSP stream if it is open.

        Explicit cleanup prevents resource leaks and releases camera handles
        cleanly during shutdown or reconnect attempts.
        """

        with self._lock:
            if self.capture.isOpened():
                self.capture.release()
                LOGGER.info("Camera stream released")