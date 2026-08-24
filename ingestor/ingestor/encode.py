"""Encoding a precipitation field into the frame format the frontend decodes.

Each pixel carries a 16-bit fraction of full-scale rain rate, split high byte
into R and low byte into G, with alpha 0 where it is dry. The frontend
recombines the bytes in a shader and applies the colour ramp on the GPU, so a
frame can be recoloured without refetching it.

Dry pixels are written as fully zeroed RGBA rather than just transparent: large
uniform runs are what make the lossless WebP small.
"""

from __future__ import annotations

import io

import numpy as np
from PIL import Image

#: Below this rate a pixel is stored as dry. Sits under the bottom of the
#: colour ramp (0.1 mm/h), so nothing visible is discarded.
DRY_THRESHOLD_MM_H = 0.05


def encode_frame(values_mm_h, max_precip_mm_h: float) -> bytes:
    """Encode a (h, w) array of mm/h into a lossless WebP frame."""
    values = np.asarray(values_mm_h, dtype=np.float32)
    wet = values >= DRY_THRESHOLD_MM_H

    scaled = np.rint(np.clip(values / max_precip_mm_h, 0.0, 1.0) * 65535.0)
    packed = np.where(wet, scaled, 0).astype(np.uint16)

    height, width = values.shape
    rgba = np.zeros((height, width, 4), dtype=np.uint8)
    rgba[:, :, 0] = (packed >> 8).astype(np.uint8)
    rgba[:, :, 1] = (packed & 0xFF).astype(np.uint8)
    rgba[:, :, 3] = np.where(wet, 255, 0).astype(np.uint8)

    buffer = io.BytesIO()
    Image.fromarray(rgba, 'RGBA').save(buffer, format='WEBP', lossless=True, quality=100, method=4)
    return buffer.getvalue()


def decode_frame(data: bytes, max_precip_mm_h: float):
    """Inverse of :func:`encode_frame`, for tests and verification."""
    rgba = np.array(Image.open(io.BytesIO(data)).convert('RGBA'))
    packed = rgba[:, :, 0].astype(np.uint32) * 256 + rgba[:, :, 1].astype(np.uint32)
    values = packed.astype(np.float32) / 65535.0 * max_precip_mm_h
    return np.where(rgba[:, :, 3] == 0, 0.0, values)
