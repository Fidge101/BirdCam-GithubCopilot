"""Timelapse sheet, GIF, and MP4 generation from captured frames.

This module provides three clear output functions:
1. Generate a contact-sheet JPEG from all frames.
2. Generate an animated GIF from all frames.
3. Generate an MP4 from all frames.
"""

from __future__ import annotations

import logging
import math
from datetime import date
from pathlib import Path

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


def _gif_frames(images: list[Image.Image]) -> list[np.ndarray]:
    """Convert prepared images into arrays suitable for GIF/MP4 writing."""

    return [np.array(image) for image in images]


def generate_timelapse_sheet_from_frames(
    frame_paths: list[Path],
    output_path: str | Path,
    columns: int = 10,
    thumbnail_size: tuple[int, int] = (320, 180),
) -> Path:
    """Generate a contact-sheet JPEG from a specific list of frame files."""

    _validate_inputs(frame_paths, thumbnail_size, columns=columns)
    output_path = Path(output_path)
    images = _prepare_images(frame_paths, thumbnail_size)

    rows = math.ceil(len(images) / columns)
    sheet = Image.new(
        "RGB",
        (thumbnail_size[0] * columns, thumbnail_size[1] * rows),
        color=(20, 20, 20),
    )

    for index, image in enumerate(images):
        x = (index % columns) * thumbnail_size[0]
        y = (index // columns) * thumbnail_size[1]
        sheet.paste(image, (x, y))

    output_path.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(output_path, format="JPEG", quality=90)
    LOGGER.info("Generated timelapse contact sheet using %s frames -> %s", len(images), output_path)
    return output_path


def generate_timelapse_gif_from_frames(
    frame_paths: list[Path],
    output_path: str | Path,
    thumbnail_size: tuple[int, int] = (320, 180),
    duration: float = 0.4,
) -> Path:
    """Generate an animated GIF from a specific list of frame files."""

    if imageio is None:
        raise RuntimeError("imageio is not available; cannot generate GIF output")

    _validate_inputs(frame_paths, thumbnail_size)
    output_path = Path(output_path)
    images = _prepare_images(frame_paths, thumbnail_size)
    gif_frames = _gif_frames(images)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    imageio.mimsave(output_path, gif_frames, duration=duration)
    LOGGER.info("Generated animated GIF using %s frames -> %s", len(images), output_path)
    return output_path


def generate_timelapse_mp4_from_frames(
    frame_paths: list[Path],
    output_path: str | Path,
    thumbnail_size: tuple[int, int] = (320, 180),
    fps: int = 8,
) -> Path:
    """Generate an MP4 from a specific list of frame files."""

    if imageio is None:
        raise RuntimeError("imageio is not available; cannot generate MP4 output")

    _validate_inputs(frame_paths, thumbnail_size)
    output_path = Path(output_path)
    images = _prepare_images(frame_paths, thumbnail_size)
    mp4_frames = _gif_frames(images)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    imageio.mimsave(output_path, mp4_frames, fps=fps)
    LOGGER.info("Generated MP4 timelapse using %s frames -> %s", len(images), output_path)
    return output_path


def generate_timelapse_sheet(
    frame_dir: str | Path,
    output_path: str | Path,
    columns: int = 10,
    thumbnail_size: tuple[int, int] = (320, 180),
) -> Path:
    """Generate a contact-sheet JPEG from all JPEG frames in a directory."""

    frame_dir = Path(frame_dir)
    frames = _load_sorted_frames(frame_dir)
    if not frames:
        raise ValueError(f"No JPEG frames found in {frame_dir}")

    return generate_timelapse_sheet_from_frames(
        frames,
        output_path,
        columns=columns,
        thumbnail_size=thumbnail_size,
    )


def generate_timelapse_gif(
    frame_dir: str | Path,
    output_path: str | Path,
    thumbnail_size: tuple[int, int] = (320, 180),
    duration: float = 0.4,
) -> Path:
    """Generate an animated GIF from all JPEG frames in a directory."""

    frame_dir = Path(frame_dir)
    frames = _load_sorted_frames(frame_dir)
    if not frames:
        raise ValueError(f"No JPEG frames found in {frame_dir}")

    return generate_timelapse_gif_from_frames(
        frames,
        output_path,
        thumbnail_size=thumbnail_size,
        duration=duration,
    )


def generate_timelapse_mp4(
    frame_dir: str | Path,
    output_path: str | Path,
    thumbnail_size: tuple[int, int] = (320, 180),
    fps: int = 8,
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
    )


def generate_timelapse_from_frames(
    frame_paths: list[Path],
    output_path: str | Path,
    columns: int = 10,
    thumbnail_size: tuple[int, int] = (320, 180),
) -> Path:
    """Generate sheet, GIF, and MP4 outputs from a specific list of frames."""

    output_path = generate_timelapse_sheet_from_frames(
        frame_paths,
        output_path,
        columns=columns,
        thumbnail_size=thumbnail_size,
    )

    if imageio is not None:
        gif_path = output_path.with_suffix(output_path.suffix + ".gif")
        generate_timelapse_gif_from_frames(frame_paths, gif_path, thumbnail_size=thumbnail_size)

        mp4_path = output_path.with_suffix(output_path.suffix + ".mp4")
        try:
            generate_timelapse_mp4_from_frames(frame_paths, mp4_path, thumbnail_size=thumbnail_size)
        except Exception as exc:  # pragma: no cover - runtime codec availability
            LOGGER.warning("Unable to generate MP4 timelapse (%s)", exc)
    else:
        LOGGER.info("imageio not available; skipping animated GIF/MP4 outputs")

    return output_path


def generate_timelapse(
    frame_dir: str | Path,
    output_path: str | Path,
    columns: int = 10,
    thumbnail_size: tuple[int, int] = (320, 180),
) -> Path:
    """Generate sheet, GIF, and MP4 outputs from all captured frames."""

    frame_dir = Path(frame_dir)
    frames = _load_sorted_frames(frame_dir)
    if not frames:
        raise ValueError(f"No JPEG frames found in {frame_dir}")

    return generate_timelapse_from_frames(frames, output_path, columns=columns, thumbnail_size=thumbnail_size)


def generate_daily_timelapse_export(
    frame_dir: str | Path,
    export_root: str | Path,
    target_date: date,
    columns: int = 10,
    thumbnail_size: tuple[int, int] = (320, 180),
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
    return generate_timelapse_from_frames(day_frames, day_output, columns=columns, thumbnail_size=thumbnail_size)