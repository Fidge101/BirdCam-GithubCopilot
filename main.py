"""CLI entry point for BirdCam live view, capture, and timelapse workflows.

This module wires the independently importable components together without
introducing side effects at import time.
"""

from __future__ import annotations

import argparse
import logging
import sys
import threading
import time
import socket

from camera import CameraStream
from config import load_config
from scheduler import run_scheduler
from timelapse import generate_timelapse
from viewer import run_live_view
from web.server import start_web_server


LOG_FORMAT = "%(asctime)s | %(levelname)s | %(name)s | %(message)s"


def configure_logging(log_file_path) -> None:
    """Configure application-wide logging with a consistent format.

    Central logging setup keeps command-line output readable and consistent
    across the viewer, scheduler, timelapse, and camera modules.
    """

    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)
    root_logger.handlers.clear()

    formatter = logging.Formatter(LOG_FORMAT)

    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setFormatter(formatter)
    root_logger.addHandler(stream_handler)

    file_handler = logging.FileHandler(log_file_path)
    file_handler.setFormatter(formatter)
    root_logger.addHandler(file_handler)


def parse_args() -> argparse.Namespace:
    """Parse CLI flags that select the BirdCam operating mode.

    Mutually exclusive flags make the supported execution modes obvious and
    prevent ambiguous combinations at startup.
    """

    parser = argparse.ArgumentParser(description="BirdCam Raspberry Pi utility")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--live", action="store_true", help="run the live viewer")
    group.add_argument("--capture", action="store_true", help="run the frame capture scheduler")
    group.add_argument("--web", action="store_true", help="run the web dashboard and capture scheduler")
    group.add_argument("--timelapse", action="store_true", help="generate a timelapse contact sheet")
    group.add_argument("--all", action="store_true", help="run live viewer and capture scheduler together")
    parser.add_argument(
        "--columns",
        type=int,
        default=10,
        help="columns to use when generating the timelapse contact sheet",
    )
    return parser.parse_args()


def _run_capture_only(camera_stream: CameraStream, config, stop_event: threading.Event) -> None:
    """Keep the scheduler alive until interrupted when capture-only mode is used.

    Returning the scheduler thread alone would let the process exit, so this
    loop keeps the main thread alive while remaining interruptible.
    """

    worker = run_scheduler(camera_stream, config, stop_event=stop_event)
    while worker.is_alive() and not stop_event.is_set():
        time.sleep(0.5)


def main() -> int:
    """Run the selected BirdCam mode and handle shutdown cleanly.

    The main function owns lifecycle concerns so each feature module can remain
    focused on one job and import cleanly in isolation.
    """

    args = parse_args()
    config = load_config()
    configure_logging(config.log_file_path)
    logger = logging.getLogger(__name__)
    stop_event = threading.Event()

    if args.timelapse:
        generate_timelapse(
            config.frame_store_dir,
            config.timelapse_output_path,
            columns=args.columns,
            thumbnail_size=(config.timelapse_width, config.timelapse_height),
        )
        return 0

    camera_stream = CameraStream(config.rtsp_url)
    if not camera_stream.connect():
        logger.error("Unable to connect to camera; exiting")
        return 1

    try:
        if args.live:
            run_live_view(camera_stream, stop_event=stop_event)
        elif args.capture:
            _run_capture_only(camera_stream, config, stop_event)
        elif args.web:
            run_scheduler(camera_stream, config, stop_event=stop_event)
            start_web_server(camera_stream, config, stop_event=stop_event)
            hostname = socket.gethostname()
            logger.info("Dashboard: http://%s.local:%s", hostname, config.port)
            while not stop_event.is_set():
                time.sleep(0.5)
        elif args.all:
            run_scheduler(camera_stream, config, stop_event=stop_event)
            start_web_server(camera_stream, config, stop_event=stop_event)
            hostname = socket.gethostname()
            logger.info("Dashboard: http://%s.local:%s", hostname, config.port)
            run_live_view(camera_stream, stop_event=stop_event)
    except KeyboardInterrupt:
        logger.info("Shutting down")
        stop_event.set()
    finally:
        stop_event.set()
        camera_stream.release()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())