"""Timelapse MP4 generation from captured frames."""

from __future__ import annotations

import logging
from datetime import date
from pathlib import Path
from collections.abc import Callable

import numpy as np
from PIL import Image


LOGGER = logging.getLogger(__name__)

try:
    import imageio.v2 as imageio
except ImportError:  # pragma: no cover - optional dependency handling
    imageio = None


def _load_sorted_frames(frame_dir: Path) -> list[Path]:
    """Return JPEG frame paths sorted chronologically by timestamp filename."""

    return sorted(frame_dir.glob("*.jpg"))


def _validate_inputs(
    frame_paths: list[Path],
    thumbnail_size: tuple[int, int],
    columns: int | None = None,
) -> None:
    """Validate common timelapse generation inputs."""

    if not frame_paths:
        raise ValueError("No frames provided for timelapse generation")
    if thumbnail_size[0] <= 0 or thumbnail_size[1] <= 0:
        raise ValueError("thumbnail_size must contain positive width and height")
    if columns is not None and columns <= 0:
        raise ValueError("columns must be greater than zero")


def _prepare_images(
    frame_paths: list[Path],
    thumbnail_size: tuple[int, int],
) -> list[Image.Image]:
    """Load frames and normalize them onto fixed-size RGB canvases."""

    _validate_inputs(frame_paths, thumbnail_size)
    images: list[Image.Image] = []

    for frame_path in frame_paths:
        with Image.open(frame_path) as image:
            prepared = image.convert("RGB")
            prepared.thumbnail(thumbnail_size)

            canvas = Image.new("RGB", thumbnail_size, color=(0, 0, 0))
            offset = (
                (thumbnail_size[0] - prepared.width) // 2,
                (thumbnail_size[1] - prepared.height) // 2,
            )
            canvas.paste(prepared, offset)
            images.append(canvas)

    return images


def _prepare_frame_array(frame_path: Path, thumbnail_size: tuple[int, int]) -> np.ndarray:
    """Load one frame, normalize it to thumbnail_size, and return RGB array."""

    with Image.open(frame_path) as image:
        prepared = image.convert("RGB")
        prepared.thumbnail(thumbnail_size)

        canvas = Image.new("RGB", thumbnail_size, color=(0, 0, 0))
        offset = (
            (thumbnail_size[0] - prepared.width) // 2,
            (thumbnail_size[1] - prepared.height) // 2,
        )
        canvas.paste(prepared, offset)
        return np.array(canvas)


def _video_frames(images: list[Image.Image]) -> list[np.ndarray]:
    """Convert prepared images into arrays suitable for MP4 writing."""

    return [np.array(image) for image in images]


def generate_timelapse_mp4_from_frames(
    frame_paths: list[Path],
    output_path: str | Path,
    thumbnail_size: tuple[int, int] = (320, 180),
    fps: int = 8,
    on_progress: Callable[[int, int], None] | None = None,
) -> Path:
    """Generate an MP4 from a specific list of frame files."""

    if imageio is None:
        raise RuntimeError("imageio is not available; cannot generate MP4 output")

    _validate_inputs(frame_paths, thumbnail_size)
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    total_frames = len(frame_paths)
    writer = imageio.get_writer(str(output_path), fps=fps, codec="libx264")
    try:
        for index, frame_path in enumerate(frame_paths, start=1):
            writer.append_data(_prepare_frame_array(frame_path, thumbnail_size))
            if on_progress is not None:
                on_progress(index, total_frames)
    finally:
        writer.close()

    LOGGER.info("Generated MP4 timelapse using %s frames -> %s", total_frames, output_path)
    return output_path


def generate_timelapse_mp4(
    frame_dir: str | Path,
    output_path: str | Path,
    thumbnail_size: tuple[int, int] = (320, 180),
    fps: int = 8,
    on_progress: Callable[[int, int], None] | None = None,
) -> Path:
    """Generate an MP4 from all JPEG frames in a directory."""

    frame_dir = Path(frame_dir)
    frames = _load_sorted_frames(frame_dir)
    if not frames:
        raise ValueError(f"No JPEG frames found in {frame_dir}")

    return generate_timelapse_mp4_from_frames(
        frames,
        output_path,
        thumbnail_size=thumbnail_size,
        fps=fps,
        on_progress=on_progress,
    )


def generate_timelapse_from_frames(
    frame_paths: list[Path],
    output_path: str | Path,
    columns: int = 10,
    thumbnail_size: tuple[int, int] = (320, 180),
    on_progress: Callable[[int, int], None] | None = None,
) -> Path:
    """Generate MP4 output from a specific list of frames.

    The output_path acts as a base path, and the generated video is written to
    `<output_path>.mp4`.
    """

    _ = columns
    base_output_path = Path(output_path)
    mp4_path = base_output_path.with_suffix(base_output_path.suffix + ".mp4")
    return generate_timelapse_mp4_from_frames(
        frame_paths,
        mp4_path,
        thumbnail_size=thumbnail_size,
        on_progress=on_progress,
    )


def generate_timelapse(
    frame_dir: str | Path,
    output_path: str | Path,
    columns: int = 10,
    thumbnail_size: tuple[int, int] = (320, 180),
    on_progress: Callable[[int, int], None] | None = None,
) -> Path:
    """Generate MP4 output from all captured frames."""

    frame_dir = Path(frame_dir)
    frames = _load_sorted_frames(frame_dir)
    if not frames:
        raise ValueError(f"No JPEG frames found in {frame_dir}")

    return generate_timelapse_from_frames(
        frames,
        output_path,
        columns=columns,
        thumbnail_size=thumbnail_size,
        on_progress=on_progress,
    )


def generate_daily_timelapse_export(
    frame_dir: str | Path,
    export_root: str | Path,
    target_date: date,
    columns: int = 10,
    thumbnail_size: tuple[int, int] = (320, 180),
    on_progress: Callable[[int, int], None] | None = None,
) -> Path:
    """Generate dated daily timelapse outputs using only one day's frames."""

    frame_dir = Path(frame_dir)
    export_root = Path(export_root)
    day_prefix = target_date.strftime("%Y%m%d_")
    day_frames = sorted(frame_dir.glob(f"{day_prefix}*.jpg"))
    if not day_frames:
        raise ValueError(f"No frames found for date {target_date.isoformat()}")

    day_dir = export_root / target_date.isoformat()
    day_output = day_dir / "timelapse.jpg"
    return generate_timelapse_from_frames(
        day_frames,
        day_output,
        columns=columns,
        thumbnail_size=thumbnail_size,
        on_progress=on_progress,
    )