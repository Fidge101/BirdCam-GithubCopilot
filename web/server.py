"""Flask server for BirdCam dashboard, MJPEG stream, and REST APIs."""

from __future__ import annotations

import logging
import socket
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import cv2
import numpy as np
from flask import Flask, Response, jsonify, request, send_file, send_from_directory
from flask_cors import CORS

from camera import CameraStream
from config import AppConfig
from scheduler import capture_single_frame
from timelapse import generate_timelapse


LOGGER = logging.getLogger(__name__)


class DashboardState:
    """Shared runtime state for dashboard metrics and background jobs.

    This state keeps endpoint handlers lightweight while avoiding global module
    variables that are hard to test or reason about.
    """

    def __init__(self) -> None:
        self.started_at = time.monotonic()
        self.streamed_frames = 0
        self.timelapse_job: dict[str, str] = {
            "status": "idle",
            "message": "No timelapse job started",
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        self._lock = threading.Lock()

    def mark_stream_frame(self) -> None:
        """Increment streamed frame count safely across request threads."""

        with self._lock:
            self.streamed_frames += 1

    def read_streamed_frames(self) -> int:
        """Return streamed frame count safely across request threads."""

        with self._lock:
            return self.streamed_frames

    def set_job(self, status: str, message: str) -> None:
        """Update timelapse background job status in a thread-safe way."""

        with self._lock:
            self.timelapse_job = {
                "status": status,
                "message": message,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }

    def get_job(self) -> dict[str, str]:
        """Read current timelapse job state in a thread-safe way."""

        with self._lock:
            return dict(self.timelapse_job)


def _offline_placeholder_frame() -> np.ndarray:
    """Build an offline placeholder frame used when RTSP frame reads fail.

    A generated frame allows MJPEG clients to keep rendering while clearly
    showing that the signal is unavailable.
    """

    frame = np.full((720, 1280, 3), 80, dtype=np.uint8)
    cv2.putText(
        frame,
        "SIGNAL LOST",
        (430, 360),
        cv2.FONT_HERSHEY_SIMPLEX,
        2.0,
        (210, 210, 210),
        4,
        cv2.LINE_AA,
    )
    return frame


def _cpu_temp_celsius() -> float | None:
    """Return Raspberry Pi CPU temperature in Celsius when available.

    Linux thermal-zone files are present on Raspberry Pi, while non-Pi systems
    should degrade gracefully by returning null in API responses.
    """

    thermal_path = Path("/sys/class/thermal/thermal_zone0/temp")
    if not thermal_path.exists():
        return None

    try:
        milli_degrees = int(thermal_path.read_text(encoding="utf-8").strip())
        return round(milli_degrees / 1000.0, 1)
    except (ValueError, OSError):
        return None


def _safe_frame_path(frame_store_dir: Path, name: str) -> Path:
    """Return a validated frame path and prevent path traversal attacks.

    Restricting to .jpg basename values keeps file serving/deletion scoped to
    the configured frame directory.
    """

    if Path(name).name != name:
        raise ValueError("Invalid frame name")
    if not name.lower().endswith(".jpg"):
        raise ValueError("Only .jpg frames are supported")
    return frame_store_dir / name


def _timestamp_from_filename(frame_name: str, frame_path: Path) -> str:
    """Extract ISO timestamp from scheduler frame names or fallback to mtime.

    Frame names are timestamp-based by default, but this fallback handles any
    older or manually-added files.
    """

    stem = Path(frame_name).stem
    try:
        parsed = datetime.strptime(stem, "%Y%m%d_%H%M%S")
        return parsed.isoformat()
    except ValueError:
        modified_at = datetime.fromtimestamp(frame_path.stat().st_mtime)
        return modified_at.isoformat()


def _update_env_file(env_path: Path, updates: dict[str, str]) -> None:
    """Persist selected config values back into the .env file in place.

    Updating only specific keys avoids rewriting unrelated configuration values
    while still allowing runtime config changes from the dashboard.
    """

    lines: list[str] = []
    seen: set[str] = set()

    if env_path.exists():
        lines = env_path.read_text(encoding="utf-8").splitlines()

    rewritten: list[str] = []
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in line:
            rewritten.append(line)
            continue

        key, _ = line.split("=", 1)
        key = key.strip()
        if key in updates:
            rewritten.append(f"{key}={updates[key]}")
            seen.add(key)
        else:
            rewritten.append(line)

    for key, value in updates.items():
        if key not in seen:
            rewritten.append(f"{key}={value}")

    env_path.write_text("\n".join(rewritten).rstrip() + "\n", encoding="utf-8")


def create_app(camera_stream: CameraStream, config: AppConfig, stop_event: threading.Event) -> Flask:
    """Create and configure the Flask dashboard application.

    The factory keeps runtime dependencies explicit, making the web layer easy
    to embed from the CLI and straightforward to test.
    """

    app = Flask(__name__, static_folder="static")
    CORS(app)
    state = DashboardState()

    @app.before_request
    def _log_request() -> None:
        """Log each HTTP request through the shared application logger."""

        LOGGER.info("HTTP %s %s from %s", request.method, request.path, request.remote_addr)

    @app.route("/")
    def dashboard() -> Response:
        """Serve the single-page BirdCam dashboard UI."""

        return send_from_directory(app.static_folder, "index.html")

    @app.get("/stream")
    def stream() -> Response:
        """Stream MJPEG frames for browser-native live video rendering."""

        def generate_mjpeg() -> bytes:
            while not stop_event.is_set():
                frame = camera_stream.read_frame()
                if frame is None:
                    frame = _offline_placeholder_frame()

                ok, encoded = cv2.imencode(
                    ".jpg",
                    frame,
                    [int(cv2.IMWRITE_JPEG_QUALITY), int(config.stream_quality)],
                )
                if not ok:
                    time.sleep(0.05)
                    continue

                state.mark_stream_frame()
                payload = encoded.tobytes()
                yield (
                    b"--frame\r\n"
                    b"Content-Type: image/jpeg\r\n\r\n" + payload + b"\r\n"
                )
                time.sleep(0.03)

        return Response(generate_mjpeg(), mimetype="multipart/x-mixed-replace; boundary=frame")

    @app.get("/api/status")
    def api_status() -> Response:
        """Return camera connectivity, uptime, frame metrics, and CPU temp."""

        uptime_seconds = int(time.monotonic() - state.started_at)
        saved_frames = len(list(config.frame_store_dir.glob("*.jpg")))
        camera_connected = camera_stream.capture.isOpened()

        return jsonify(
            {
                "camera_connected": camera_connected,
                "uptime_seconds": uptime_seconds,
                "frame_count": saved_frames,
                "streamed_frames": state.read_streamed_frames(),
                "cpu_temp_c": _cpu_temp_celsius(),
                "hostname": socket.gethostname(),
                "timelapse_job": state.get_job(),
            }
        )

    @app.route("/api/frames", methods=["GET", "DELETE"])
    def api_frames() -> Response:
        """List captured frames or clear all frames based on HTTP method."""

        if request.method == "DELETE":
            deleted = 0
            for frame_path in config.frame_store_dir.glob("*.jpg"):
                frame_path.unlink(missing_ok=True)
                deleted += 1
            LOGGER.info("Cleared %s frames from %s", deleted, config.frame_store_dir)
            return jsonify({"ok": True, "deleted": deleted})

        frames = []
        for frame_path in sorted(config.frame_store_dir.glob("*.jpg"), reverse=True):
            stat = frame_path.stat()
            frames.append(
                {
                    "filename": frame_path.name,
                    "timestamp": _timestamp_from_filename(frame_path.name, frame_path),
                    "size_kb": round(stat.st_size / 1024.0, 1),
                }
            )
        return jsonify(frames)

    @app.get("/api/frames/<name>")
    def api_frame_get(name: str) -> Response:
        """Serve one captured frame image by filename."""

        try:
            frame_path = _safe_frame_path(config.frame_store_dir, name)
        except ValueError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400

        if not frame_path.exists():
            return jsonify({"ok": False, "error": "Frame not found"}), 404
        return send_file(frame_path)

    @app.delete("/api/frames/<name>")
    def api_frame_delete(name: str) -> Response:
        """Delete one captured frame image by filename."""

        try:
            frame_path = _safe_frame_path(config.frame_store_dir, name)
        except ValueError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400

        if not frame_path.exists():
            return jsonify({"ok": False, "error": "Frame not found"}), 404

        frame_path.unlink(missing_ok=True)
        LOGGER.info("Deleted frame %s", frame_path)
        return jsonify({"ok": True, "deleted": name})

    @app.post("/api/capture")
    def api_capture() -> Response:
        """Trigger immediate single-frame capture and return saved filename."""

        output_path = capture_single_frame(camera_stream, config)
        if output_path is None:
            return jsonify({"ok": False, "error": "No frame available"}), 503
        return jsonify({"ok": True, "filename": output_path.name})

    @app.get("/api/timelapse/preview")
    def api_timelapse_preview() -> Response:
        """Serve the timelapse JPEG contact sheet when it exists."""

        if not config.timelapse_output_path.exists():
            return jsonify({"ok": False, "error": "Timelapse preview not found"}), 404
        return send_file(config.timelapse_output_path)

    @app.post("/api/timelapse/generate")
    def api_timelapse_generate() -> Response:
        """Start timelapse generation job and return current job status."""

        current = state.get_job()
        if current["status"] == "running":
            return jsonify({"ok": True, **current})

        def job() -> None:
            state.set_job("running", "Generating timelapse")
            try:
                output_path = generate_timelapse(
                    config.frame_store_dir,
                    config.timelapse_output_path,
                    columns=10,
                )
                state.set_job("completed", f"Timelapse generated at {output_path}")
            except Exception as exc:  # pragma: no cover - defensive logging
                LOGGER.exception("Timelapse generation failed")
                state.set_job("error", str(exc))

        threading.Thread(target=job, name="timelapse-job", daemon=True).start()
        return jsonify({"ok": True, **state.get_job()})

    @app.route("/api/config", methods=["GET", "POST"])
    def api_config() -> Response:
        """Read current config values or update allowed mutable settings."""

        if request.method == "GET":
            return jsonify(
                {
                    "camera_ip": config.camera_ip,
                    "camera_user": config.camera_user,
                    "camera_pass": "***",
                    "capture_interval_seconds": config.capture_interval_seconds,
                    "max_frames": config.max_frames,
                    "stream_quality": config.stream_quality,
                    "port": config.port,
                    "frame_store_dir": str(config.frame_store_dir),
                    "timelapse_output_path": str(config.timelapse_output_path),
                }
            )

        payload = request.get_json(silent=True) or {}
        try:
            capture_interval = int(payload["capture_interval_seconds"])
            max_frames = int(payload["max_frames"])
        except (KeyError, TypeError, ValueError):
            return jsonify({"ok": False, "error": "capture_interval_seconds and max_frames must be integers"}), 400

        if capture_interval < 10 or capture_interval > 3600:
            return jsonify({"ok": False, "error": "capture_interval_seconds must be between 10 and 3600"}), 400
        if max_frames <= 0:
            return jsonify({"ok": False, "error": "max_frames must be greater than zero"}), 400

        config.capture_interval_seconds = capture_interval
        config.max_frames = max_frames
        _update_env_file(
            Path(".env"),
            {
                "CAPTURE_INTERVAL_SECONDS": str(capture_interval),
                "MAX_FRAMES": str(max_frames),
            },
        )
        LOGGER.info("Updated runtime config: capture_interval_seconds=%s, max_frames=%s", capture_interval, max_frames)
        return jsonify({"ok": True, "capture_interval_seconds": capture_interval, "max_frames": max_frames})

    @app.get("/api/logs/stream")
    def api_logs_stream() -> Response:
        """Stream appended application log lines via server-sent events."""

        def tail_logs() -> bytes:
            log_path = config.log_file_path
            log_path.touch(exist_ok=True)
            with log_path.open("r", encoding="utf-8", errors="replace") as handle:
                handle.seek(0, 2)
                while not stop_event.is_set():
                    line = handle.readline()
                    if line:
                        message = line.rstrip("\n").replace("\r", "")
                        yield f"data: {message}\n\n"
                    else:
                        time.sleep(0.25)

        return Response(tail_logs(), mimetype="text/event-stream")

    return app


def start_web_server(camera_stream: CameraStream, config: AppConfig, stop_event: threading.Event) -> threading.Thread:
    """Start Flask dashboard server in a daemon thread and return the thread.

    Threaded startup allows the CLI to run capture and live-view workflows while
    serving the dashboard concurrently.
    """

    app = create_app(camera_stream, config, stop_event)

    def _run() -> None:
        app.run(host="0.0.0.0", port=config.port, threaded=True, use_reloader=False)

    worker = threading.Thread(target=_run, name="birdcam-web-server", daemon=True)
    worker.start()
    return worker
