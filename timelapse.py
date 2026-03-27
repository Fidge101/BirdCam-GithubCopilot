"""Contact sheet and optional GIF generation from captured frames.

This module turns many JPEG snapshots into a single reviewable image that is
more practical than video on a mostly headless Raspberry Pi workflow.
"""

from __future__ import annotations

import logging
import math
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


def generate_timelapse(frame_dir: str | Path, output_path: str | Path, columns: int = 10) -> Path:
    """Build a thumbnail contact sheet and optional GIF from captured frames.

    A contact sheet provides a fast visual summary of a session and is easier to
    inspect remotely than a full motion video on constrained devices.
    """

    frame_dir = Path(frame_dir)
    output_path = Path(output_path)
    frames = _load_sorted_frames(frame_dir)

    if columns <= 0:
        raise ValueError("columns must be greater than zero")
    if not frames:
        raise ValueError(f"No JPEG frames found in {frame_dir}")

    thumbnail_size = (320, 180)
    images: list[Image.Image] = []
    gif_frames = []

    for frame_path in frames:
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

            if imageio is not None:
                gif_frames.append(np.array(canvas))

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
        gif_path = output_path.with_suffix(output_path.suffix + ".gif")
        imageio.mimsave(gif_path, gif_frames, duration=0.4)
        LOGGER.info("Generated animated GIF -> %s", gif_path)
    else:
        LOGGER.info("imageio not available; skipping animated GIF output")

    return output_path