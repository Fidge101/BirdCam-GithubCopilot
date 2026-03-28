"""Contact sheet and optional GIF/MP4 generation from captured frames.

This module turns many JPEG snapshots into a single reviewable image that is
more practical than video on a mostly headless Raspberry Pi workflow.
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
    """Return JPEG frame paths sorted chronologically by timestamp filename.

    The scheduler names files with sortable timestamps, so filename ordering is
    sufficient to preserve capture chronology without extra metadata.
    """

    return sorted(frame_dir.glob("*.jpg"))


def _export_sheet_and_animations(
    images: list[Image.Image],
    output_path: Path,
    columns: int,
    thumbnail_size: tuple[int, int],
) -> None:
    """Write contact-sheet JPEG and optional GIF/MP4 outputs from prepared frames."""

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

    if imageio is not None:
        gif_frames = [np.array(image) for image in images]
        gif_path = output_path.with_suffix(output_path.suffix + ".gif")
        imageio.mimsave(gif_path, gif_frames, duration=0.4)
        LOGGER.info("Generated animated GIF -> %s", gif_path)

        mp4_path = output_path.with_suffix(output_path.suffix + ".mp4")
        try:
            imageio.mimsave(mp4_path, gif_frames, fps=8)
            LOGGER.info("Generated MP4 timelapse -> %s", mp4_path)
        except Exception as exc:  # pragma: no cover - runtime codec availability
            LOGGER.warning("Unable to generate MP4 timelapse (%s)", exc)
    else:
        LOGGER.info("imageio not available; skipping animated GIF/MP4 outputs")


def generate_timelapse_from_frames(
    frame_paths: list[Path],
    output_path: str | Path,
    columns: int = 10,
    thumbnail_size: tuple[int, int] = (320, 180),
) -> Path:
    """Generate timelapse outputs from a specific list of frame files."""

    output_path = Path(output_path)
    if columns <= 0:
        raise ValueError("columns must be greater than zero")
    if not frame_paths:
        raise ValueError("No frames provided for timelapse generation")

    if thumbnail_size[0] <= 0 or thumbnail_size[1] <= 0:
        raise ValueError("thumbnail_size must contain positive width and height")
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

    _export_sheet_and_animations(images, output_path, columns, thumbnail_size)
    return output_path


def generate_timelapse(
    frame_dir: str | Path,
    output_path: str | Path,
    columns: int = 10,
    thumbnail_size: tuple[int, int] = (320, 180),
) -> Path:
    """Build a thumbnail contact sheet and optional GIF/MP4 from captured frames.

    A contact sheet provides a fast visual summary of a session and is easier to
    inspect remotely than a full motion video on constrained devices.
    """

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