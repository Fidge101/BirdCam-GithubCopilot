"""Flask server for BirdCam dashboard, MJPEG stream, and REST APIs."""

from __future__ import annotations

import logging
import socket
import threading
import time
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4
from collections import deque
from collections.abc import Callable

import cv2
import numpy as np
from flask import Flask, Response, jsonify, request, send_file, send_from_directory
from flask_cors import CORS

from camera import CameraStream
from config import AppConfig
from scheduler import capture_single_frame
from timelapse import generate_timelapse

try:
    import imageio.v2 as imageio
except ImportError:  # pragma: no cover - optional dependency fallback
    imageio = None

try:
    from imageio_ffmpeg import get_ffmpeg_exe
except ImportError:  # pragma: no cover - optional dependency fallback
    get_ffmpeg_exe = None


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
        self.merge_job: dict[str, object] = {
            "job_id": None,
            "status": "idle",
            "message": "No merge job started",
            "progress": 0,
            "start": None,
            "end": None,
            "video_count": 0,
            "download_url": None,
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

    def set_merge_job(self, payload: dict[str, object]) -> None:
        """Set stitched export merge job state in a thread-safe way."""

        with self._lock:
            merged_payload = {
                "job_id": None,
                "status": "idle",
                "message": "No merge job started",
                "progress": 0,
                "start": None,
                "end": None,
                "video_count": 0,
                "download_url": None,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }
            merged_payload.update(payload)
            merged_payload["updated_at"] = datetime.now(timezone.utc).isoformat()
            self.merge_job = merged_payload

    def update_merge_job(self, updates: dict[str, object]) -> None:
        """Apply partial updates to stitched export merge job state."""

        with self._lock:
            self.merge_job.update(updates)
            self.merge_job["updated_at"] = datetime.now(timezone.utc).isoformat()

    def get_merge_job(self) -> dict[str, object]:
        """Read stitched export merge job state in a thread-safe way."""

        with self._lock:
            return dict(self.merge_job)


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


def _parse_iso_datetime(value: str | None, field_name: str) -> datetime | None:
    """Parse an ISO-8601 datetime string for API filtering parameters."""

    if value is None or not value.strip():
        return None
    try:
        return datetime.fromisoformat(value.strip())
    except ValueError as exc:
        raise ValueError(f"Invalid datetime for {field_name}: {value}") from exc


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


def _safe_export_file_path(export_root: Path, relative_path: str) -> Path:
    """Resolve and validate a daily-export file path under export root."""

    candidate = (export_root / relative_path).resolve()
    if export_root not in candidate.parents and candidate != export_root:
        raise ValueError("Invalid export path")
    return candidate


def _stitch_mp4_files(
    video_paths: list[Path],
    output_path: Path,
    on_video_started: Callable[[int, int, Path], None] | None = None,
    on_video_completed: Callable[[int, int, Path], None] | None = None,
) -> None:
    """Concatenate multiple MP4 files into one MP4 by appending decoded frames."""

    if imageio is None:
        raise RuntimeError("imageio is required for MP4 stitching")

    fps = 8
    first_reader = imageio.get_reader(str(video_paths[0]))
    try:
        metadata = first_reader.get_meta_data() or {}
        fps = int(metadata.get("fps", 8))
    finally:
        first_reader.close()

    writer = imageio.get_writer(str(output_path), fps=fps, codec="libx264")
    try:
        for index, video_path in enumerate(video_paths, start=1):
            if on_video_started is not None:
                on_video_started(index, len(video_paths), video_path)
            reader = imageio.get_reader(str(video_path))
            try:
                for frame in reader:
                    writer.append_data(frame)
            finally:
                reader.close()
            if on_video_completed is not None:
                on_video_completed(index, len(video_paths), video_path)
    finally:
        writer.close()


def _concat_mp4_files_ffmpeg(video_paths: list[Path], output_path: Path) -> None:
    """Concatenate MP4 files using ffmpeg concat demuxer with stream copy."""

    if get_ffmpeg_exe is None:
        raise RuntimeError("imageio-ffmpeg is required for ffmpeg-based concatenation")

    ffmpeg_exe = get_ffmpeg_exe()
    list_file = output_path.with_suffix(".txt")
    list_content = "\n".join([f"file '{path.resolve()}'" for path in video_paths]) + "\n"
    list_file.write_text(list_content, encoding="utf-8")

    try:
        command = [
            ffmpeg_exe,
            "-y",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(list_file),
            "-c",
            "copy",
            str(output_path),
        ]
        completed = subprocess.run(command, capture_output=True, text=True, check=False)
        if completed.returncode != 0:
            raise RuntimeError((completed.stderr or completed.stdout or "ffmpeg concat failed").strip())
    finally:
        list_file.unlink(missing_ok=True)


def create_app(
    camera_stream: CameraStream,
    config: AppConfig,
    stop_event: threading.Event,
    sd_camera_stream: CameraStream | None = None,
) -> Flask:
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

    @app.route("/viewer")
    def image_viewer() -> Response:
        """Serve the dedicated image viewer page with time filters."""

        return send_from_directory(app.static_folder, "viewer.html")

    @app.route("/live")
    def live_viewer() -> Response:
        """Serve on-demand high-resolution live MJPEG viewer page."""

        return send_from_directory(app.static_folder, "live.html")

    def _mjpeg_response(stream_source: CameraStream) -> Response:
        """Build an MJPEG streaming response from a camera source."""

        def generate_mjpeg() -> bytes:
            while not stop_event.is_set():
                frame = stream_source.read_frame()
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

    @app.get("/stream")
    def stream() -> Response:
        """Stream MJPEG frames for high-resolution browser live view."""

        return _mjpeg_response(camera_stream)

    @app.get("/stream/sd")
    def stream_sd() -> Response:
        """Stream MJPEG frames for lower-resolution dashboard live view."""

        source = sd_camera_stream if sd_camera_stream is not None else camera_stream
        return _mjpeg_response(source)

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

        try:
            start_at = _parse_iso_datetime(request.args.get("start"), "start")
            end_at = _parse_iso_datetime(request.args.get("end"), "end")
        except ValueError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400

        limit_value = request.args.get("limit")
        limit = None
        if limit_value is not None and limit_value.strip():
            try:
                limit = int(limit_value)
            except ValueError:
                return jsonify({"ok": False, "error": "limit must be an integer"}), 400
            if limit <= 0:
                return jsonify({"ok": False, "error": "limit must be greater than zero"}), 400

        frames = []
        for frame_path in sorted(config.frame_store_dir.glob("*.jpg"), reverse=True):
            stat = frame_path.stat()
            safe_name = frame_path.name
            timestamp_iso = _timestamp_from_filename(safe_name, frame_path)
            frame_timestamp = datetime.fromisoformat(timestamp_iso)

            if start_at is not None and frame_timestamp < start_at:
                continue
            if end_at is not None and frame_timestamp > end_at:
                continue

            frames.append(
                {
                    "filename": safe_name,
                    "timestamp": timestamp_iso,
                    "size_kb": round(stat.st_size / 1024.0, 1),
                    "url": f"/api/frames/{safe_name}",
                }
            )
            if limit is not None and len(frames) >= limit:
                break
        return jsonify(frames)

    @app.get("/api/frames/<path:name>")
    def api_frame_get(name: str) -> Response:
        """Serve one captured frame image by filename."""

        try:
            frame_path = _safe_frame_path(config.frame_store_dir, name)
        except ValueError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400

        if not frame_path.exists():
            return jsonify({"ok": False, "error": "Frame not found"}), 404
        return send_file(frame_path, mimetype="image/jpeg")

    @app.delete("/api/frames/<path:name>")
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

    @app.post("/api/camera/reconnect")
    def api_camera_reconnect() -> Response:
        """Force RTSP camera reconnect and return updated connection status."""

        LOGGER.warning("Manual camera reconnect requested from dashboard")
        success = camera_stream.reconnect()
        if not success:
            return jsonify({"ok": False, "error": "Unable to reconnect camera stream"}), 503
        return jsonify({"ok": True, "message": "Camera stream reconnected"})

    @app.get("/api/timelapse")
    def api_timelapse_meta() -> Response:
        """Return timelapse MP4 metadata and direct download URL."""

        mp4_path = config.timelapse_output_path.with_suffix(config.timelapse_output_path.suffix + ".mp4")
        mp4_exists = mp4_path.exists()

        return jsonify(
            {
                "mp4_exists": mp4_exists,
                "mp4_url": "/api/timelapse/file/mp4",
                "mp4_name": mp4_path.name,
            }
        )

    @app.get("/api/timelapse/exports/dates")
    def api_timelapse_export_dates() -> Response:
        """List available daily export dates and summary file counts."""

        export_root = config.daily_export_dir
        export_root.mkdir(parents=True, exist_ok=True)

        dates: list[dict[str, object]] = []
        for day_dir in sorted((path for path in export_root.iterdir() if path.is_dir()), reverse=True):
            try:
                datetime.strptime(day_dir.name, "%Y-%m-%d")
            except ValueError:
                continue

            files = [path for path in day_dir.iterdir() if path.is_file()]
            has_mp4 = any(path.name.endswith(".mp4") for path in files)
            dates.append(
                {
                    "date": day_dir.name,
                    "file_count": len(files),
                    "has_mp4": has_mp4,
                }
            )

        return jsonify(dates)

    @app.get("/api/timelapse/exports")
    def api_timelapse_exports_for_date() -> Response:
        """List timelapse export files for a specific date folder."""

        date_value = request.args.get("date", "").strip()
        if not date_value:
            return jsonify({"ok": False, "error": "date query parameter is required"}), 400
        try:
            datetime.strptime(date_value, "%Y-%m-%d")
        except ValueError:
            return jsonify({"ok": False, "error": "date must be in YYYY-MM-DD format"}), 400

        day_dir = config.daily_export_dir / date_value
        if not day_dir.exists() or not day_dir.is_dir():
            return jsonify([])

        files = []
        for file_path in sorted((path for path in day_dir.iterdir() if path.is_file())):
            relative_path = f"{date_value}/{file_path.name}"
            files.append(
                {
                    "name": file_path.name,
                    "date": date_value,
                    "size_kb": round(file_path.stat().st_size / 1024.0, 1),
                    "modified": datetime.fromtimestamp(file_path.stat().st_mtime).isoformat(),
                    "url": f"/api/timelapse/exports/file/{relative_path}",
                }
            )

        return jsonify(files)

    @app.get("/api/timelapse/exports/file/<path:relative_path>")
    def api_timelapse_export_file(relative_path: str) -> Response:
        """Serve a specific file from a dated daily timelapse export folder."""

        try:
            file_path = _safe_export_file_path(config.daily_export_dir, relative_path)
        except ValueError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400

        if not file_path.exists() or not file_path.is_file():
            return jsonify({"ok": False, "error": "Export file not found"}), 404

        suffix = file_path.suffix.lower()
        if suffix == ".jpg":
            mimetype = "image/jpeg"
        elif suffix == ".gif":
            mimetype = "image/gif"
        elif suffix == ".mp4":
            mimetype = "video/mp4"
        else:
            mimetype = "application/octet-stream"

        return send_file(file_path, mimetype=mimetype)

    @app.post("/api/timelapse/exports/merge/start")
    def api_timelapse_export_merge_start() -> Response:
        """Start async merge job for daily MP4 exports in a date range."""

        payload = request.get_json(silent=True) or {}
        start_value = str(payload.get("start", "")).strip()
        end_value = str(payload.get("end", "")).strip()
        if not start_value or not end_value:
            return jsonify({"ok": False, "error": "start and end are required"}), 400

        try:
            start_date = datetime.strptime(start_value, "%Y-%m-%d").date()
            end_date = datetime.strptime(end_value, "%Y-%m-%d").date()
        except ValueError:
            return jsonify({"ok": False, "error": "start and end must be in YYYY-MM-DD format"}), 400

        if end_date < start_date:
            return jsonify({"ok": False, "error": "end date must be on or after start date"}), 400

        current_merge = state.get_merge_job()
        if current_merge.get("status") == "running":
            return jsonify({"ok": False, "error": "A merge job is already running", **current_merge}), 409

        selected_videos: list[Path] = []
        current = start_date
        while current <= end_date:
            day_dir = config.daily_export_dir / current.isoformat()
            day_mp4 = day_dir / "timelapse.jpg.mp4"
            if day_mp4.exists():
                selected_videos.append(day_mp4)
            current += timedelta(days=1)

        if not selected_videos:
            return jsonify({"ok": False, "error": "No MP4 exports found in selected range"}), 404

        merged_dir = config.daily_export_dir / "_merged"
        merged_dir.mkdir(parents=True, exist_ok=True)
        job_id = uuid4().hex[:12]
        merged_name = f"timelapse_{start_date.isoformat()}_to_{end_date.isoformat()}_{job_id}.mp4"
        merged_path = merged_dir / merged_name

        state.set_merge_job(
            {
                "job_id": job_id,
                "status": "running",
                "message": "Starting merge",
                "progress": 0,
                "start": start_date.isoformat(),
                "end": end_date.isoformat(),
                "video_count": len(selected_videos),
                "download_url": None,
                "output_path": str(merged_path),
            }
        )

        def merge_job() -> None:
            if len(selected_videos) == 1:
                state.update_merge_job(
                    {
                        "message": f"Using single source video: {selected_videos[0].name}",
                        "progress": 90,
                    }
                )
                merged_path.write_bytes(selected_videos[0].read_bytes())
                state.update_merge_job(
                    {
                        "status": "completed",
                        "message": "Merge complete",
                        "progress": 100,
                        "download_url": f"/api/timelapse/exports/merge/download/{job_id}",
                    }
                )
                return

            try:
                state.update_merge_job({"message": "Concatenating MP4 files", "progress": 10})
                try:
                    _concat_mp4_files_ffmpeg(selected_videos, merged_path)
                except Exception as concat_exc:
                    LOGGER.warning("ffmpeg concat failed, falling back to re-encode: %s", concat_exc)

                    def on_started(index: int, total: int, path: Path) -> None:
                        percent = max(10, int(((index - 1) / total) * 80) + 10)
                        state.update_merge_job(
                            {
                                "message": f"Re-encoding {index}/{total}: {path.name}",
                                "progress": percent,
                            }
                        )

                    def on_completed(index: int, total: int, path: Path) -> None:
                        percent = max(15, int((index / total) * 90))
                        state.update_merge_job(
                            {
                                "message": f"Re-encoded {index}/{total}: {path.name}",
                                "progress": percent,
                            }
                        )

                    _stitch_mp4_files(
                        selected_videos,
                        merged_path,
                        on_video_started=on_started,
                        on_video_completed=on_completed,
                    )
                state.update_merge_job(
                    {
                        "status": "completed",
                        "message": "Merge complete",
                        "progress": 100,
                        "download_url": f"/api/timelapse/exports/merge/download/{job_id}",
                    }
                )
            except Exception as exc:
                LOGGER.exception("Date-range MP4 merge failed")
                state.update_merge_job(
                    {
                        "status": "error",
                        "message": f"Merge failed: {exc}",
                        "progress": 0,
                        "download_url": None,
                    }
                )

        threading.Thread(target=merge_job, name=f"timelapse-merge-{job_id}", daemon=True).start()

        return jsonify({"ok": True, **state.get_merge_job()})

    @app.get("/api/timelapse/exports/merge/status")
    def api_timelapse_export_merge_status() -> Response:
        """Return current async stitched export merge job state."""

        return jsonify(state.get_merge_job())

    @app.get("/api/timelapse/exports/merge/download/<job_id>")
    def api_timelapse_export_merge_download(job_id: str) -> Response:
        """Download completed stitched MP4 by merge job id."""

        merge_job = state.get_merge_job()
        if merge_job.get("job_id") != job_id:
            return jsonify({"ok": False, "error": "Merge job not found"}), 404
        if merge_job.get("status") != "completed":
            return jsonify({"ok": False, "error": "Merge job is not completed"}), 409

        output_path_value = merge_job.get("output_path")
        if not output_path_value:
            return jsonify({"ok": False, "error": "Merged output path missing"}), 500

        output_path = Path(str(output_path_value))
        if not output_path.exists():
            return jsonify({"ok": False, "error": "Merged output not found"}), 404

        return send_file(output_path, mimetype="video/mp4", as_attachment=True, download_name=output_path.name)

    @app.get("/api/timelapse/file/mp4")
    def api_timelapse_file_mp4() -> Response:
        """Serve generated timelapse MP4 when available."""

        mp4_path = config.timelapse_output_path.with_suffix(config.timelapse_output_path.suffix + ".mp4")
        if not mp4_path.exists():
            return jsonify({"ok": False, "error": "Timelapse MP4 not found"}), 404
        return send_file(mp4_path, mimetype="video/mp4")

    @app.get("/api/timelapse/file/mp4/download")
    def api_timelapse_file_mp4_download() -> Response:
        """Download generated timelapse MP4 as an attachment."""

        mp4_path = config.timelapse_output_path.with_suffix(config.timelapse_output_path.suffix + ".mp4")
        if not mp4_path.exists():
            return jsonify({"ok": False, "error": "Timelapse MP4 not found"}), 404
        return send_file(mp4_path, mimetype="video/mp4", as_attachment=True, download_name=mp4_path.name)

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
                    thumbnail_size=(config.timelapse_width, config.timelapse_height),
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
                    "timelapse_width": config.timelapse_width,
                    "timelapse_height": config.timelapse_height,
                    "blank_frame_reconnect_threshold": config.blank_frame_reconnect_threshold,
                }
            )

        payload = request.get_json(silent=True) or {}
        try:
            capture_interval = int(payload["capture_interval_seconds"])
            max_frames = int(payload["max_frames"])
            timelapse_width = int(payload.get("timelapse_width", config.timelapse_width))
            timelapse_height = int(payload.get("timelapse_height", config.timelapse_height))
        except (KeyError, TypeError, ValueError):
            return jsonify(
                {
                    "ok": False,
                    "error": "capture_interval_seconds, max_frames, timelapse_width, and timelapse_height must be integers",
                }
            ), 400

        if capture_interval < 10 or capture_interval > 3600:
            return jsonify({"ok": False, "error": "capture_interval_seconds must be between 10 and 3600"}), 400
        if max_frames <= 0:
            return jsonify({"ok": False, "error": "max_frames must be greater than zero"}), 400
        if timelapse_width < 64 or timelapse_width > 3840:
            return jsonify({"ok": False, "error": "timelapse_width must be between 64 and 3840"}), 400
        if timelapse_height < 64 or timelapse_height > 2160:
            return jsonify({"ok": False, "error": "timelapse_height must be between 64 and 2160"}), 400

        config.capture_interval_seconds = capture_interval
        config.max_frames = max_frames
        config.timelapse_width = timelapse_width
        config.timelapse_height = timelapse_height
        _update_env_file(
            config.env_path,
            {
                "CAPTURE_INTERVAL_SECONDS": str(capture_interval),
                "MAX_FRAMES": str(max_frames),
                "TIMELAPSE_WIDTH": str(timelapse_width),
                "TIMELAPSE_HEIGHT": str(timelapse_height),
            },
        )
        LOGGER.info(
            "Updated runtime config: capture_interval_seconds=%s, max_frames=%s, timelapse=%sx%s",
            capture_interval,
            max_frames,
            timelapse_width,
            timelapse_height,
        )
        return jsonify(
            {
                "ok": True,
                "capture_interval_seconds": capture_interval,
                "max_frames": max_frames,
                "timelapse_width": timelapse_width,
                "timelapse_height": timelapse_height,
            }
        )

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

    @app.get("/api/logs/recent")
    def api_logs_recent() -> Response:
        """Return the most recent log lines for initial dashboard preload."""

        limit_value = request.args.get("limit", "100").strip()
        try:
            limit = int(limit_value)
        except ValueError:
            return jsonify({"ok": False, "error": "limit must be an integer"}), 400

        if limit <= 0:
            return jsonify({"ok": False, "error": "limit must be greater than zero"}), 400
        limit = min(limit, 500)

        log_path = config.log_file_path
        log_path.touch(exist_ok=True)

        recent = deque(maxlen=limit)
        with log_path.open("r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                stripped = line.rstrip("\n").replace("\r", "")
                if stripped:
                    recent.append(stripped)

        return jsonify({"lines": list(recent)})

    return app


def start_web_server(
    camera_stream: CameraStream,
    config: AppConfig,
    stop_event: threading.Event,
    sd_camera_stream: CameraStream | None = None,
) -> threading.Thread:
    """Start Flask dashboard server in a daemon thread and return the thread.

    Threaded startup allows the CLI to run capture and live-view workflows while
    serving the dashboard concurrently.
    """

    app = create_app(camera_stream, config, stop_event, sd_camera_stream=sd_camera_stream)

    def _run() -> None:
        app.run(host="0.0.0.0", port=config.port, threaded=True, use_reloader=False)

    worker = threading.Thread(target=_run, name="birdcam-web-server", daemon=True)
    worker.start()
    return worker
