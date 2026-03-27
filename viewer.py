"""Live viewer for displaying the camera stream in a window.

This module is separate from the scheduler so the interactive display can be
run independently from background frame capture.
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime

import cv2

from camera import CameraStream


LOGGER = logging.getLogger(__name__)


def run_live_view(camera_stream: CameraStream, stop_event: threading.Event | None = None) -> None:
    """Display the live feed with a timestamp overlay until the user quits.

    Keeping the viewer isolated from scheduling logic makes it usable on its
    own for installation checks, framing, and diagnostics.
    """

    if stop_event is None:
        stop_event = threading.Event()

    LOGGER.info("Starting live viewer; press 'q' in the viewer window to quit")

    try:
        while not stop_event.is_set():
            frame = camera_stream.read_frame()
            if frame is None:
                LOGGER.warning("Skipping live view update because no frame was received")
                continue

            timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            cv2.putText(
                frame,
                timestamp,
                (10, 30),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.8,
                (0, 255, 0),
                2,
                cv2.LINE_AA,
            )
            cv2.imshow("BirdCam Live View", frame)

            if cv2.waitKey(1) & 0xFF == ord("q"):
                LOGGER.info("Live viewer received quit command")
                stop_event.set()
                break
    finally:
        cv2.destroyAllWindows()
        LOGGER.info("Live viewer stopped")