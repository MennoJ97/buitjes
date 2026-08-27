"""Encoding a precipitation field into the frame format the frontend decodes.

Each pixel carries a 16-bit fraction of full-scale rain rate, split high byte
into R and low byte into G, with alpha 0 where it is dry. The frontend
recombines the bytes in a shader and applies the colour ramp on the GPU, so a
frame can be recoloured without refetching it.

Blue carries a flag rather than data: 255 marks a pixel no radar measured, as
distinct from one measured and found dry. Both are invisible on the map - there
is nothing to draw either way - but they are different answers to "is it raining
there", and the readout under the cursor says so. Observed frames are the ones
that need it; a forecast covers its whole domain by construction.

Dry pixels are written as fully zeroed RGBA rather than just transparent: large
uniform runs are what make the lossless WebP small. Unmeasured pixels are a
uniform run of their own, so the flag costs essentially nothing.
"""

from __future__ import annotations

import io

import numpy as np
from PIL import Image

#: Below this rate a pixel is stored as dry. Sits under the bottom of the
#: colour ramp (0.1 mm/h), so nothing visible is discarded.
DRY_THRESHOLD_MM_H = 0.05

#: Blue channel value marking a pixel no radar looked at.
NO_DATA_FLAG = 255

def encode_frame(values_mm_h, max_precip_mm_h: float, measured=None) -> bytes:
    """Encode a (h, w) array of mm/h into a lossless WebP frame.

    ``measured`` is an optional boolean mask; where it is False the pixel is
    flagged as unmeasured instead of dry.
    """
    values = np.asarray(values_mm_h, dtype=np.float32)
    wet = values >= DRY_THRESHOLD_MM_H
    if measured is not None:
        measured = np.asarray(measured, dtype=bool)
        wet = wet & measured

    scaled = np.rint(np.clip(values / max_precip_mm_h, 0.0, 1.0) * 65535.0)
    packed = np.where(wet, scaled, 0).astype(np.uint16)

    height, width = values.shape
    rgba = np.zeros((height, width, 4), dtype=np.uint8)
    rgba[:, :, 0] = (packed >> 8).astype(np.uint8)
    rgba[:, :, 1] = (packed & 0xFF).astype(np.uint8)
    if measured is not None:
        rgba[:, :, 2] = np.where(measured, 0, NO_DATA_FLAG).astype(np.uint8)
    rgba[:, :, 3] = np.where(wet, 255, 0).astype(np.uint8)

    buffer = io.BytesIO()
    # exact=True, or WebP rewrites the colour under fully transparent pixels to
    # whatever compresses best - which is a fine trade for a picture and a
    # silent data loss for us, since the no-data flag lives in exactly those
    # pixels. It costs a percent or two of frame size.
    Image.fromarray(rgba, 'RGBA').save(
        buffer, format='WEBP', lossless=True, quality=100, method=4, exact=True
    )
    return buffer.getvalue()


def decode_frame(data: bytes, max_precip_mm_h: float):
    """Inverse of :func:`encode_frame`, for tests and verification.

    Returns ``(values, measured)``, matching what was encoded.
    """
    rgba = np.array(Image.open(io.BytesIO(data)).convert('RGBA'))
    packed = rgba[:, :, 0].astype(np.uint32) * 256 + rgba[:, :, 1].astype(np.uint32)
    values = packed.astype(np.float32) / 65535.0 * max_precip_mm_h
    measured = rgba[:, :, 2] < NO_DATA_FLAG
    return np.where(rgba[:, :, 3] == 0, 0.0, values), measured


# -------------------------------------------------------------------- spread

#: Bottom of the log scale the spread frames use, matching the bottom of the
#: colour ramp. Below it there is nothing to draw, so nothing to resolve.
SPREAD_FLOOR_MM_H = 0.1

#: Byte 0 means dry; 1..255 span the scale, so 255 steps carry the decades.
_SPREAD_LEVELS = 255


def encode_spread_frame(fields, max_precip_mm_h: float) -> bytes:
    """Encode three rate fields into one lossless WebP, a byte each.

    A rate fits in a byte if the scale is logarithmic. The rain frames spend
    sixteen bits on a *linear* scale, which is the wasteful way round: at the
    bottom of the ramp a step is 0.0015 mm/h, finer than KNMI's own 0.01 mm/h
    quantisation, and at 90 mm/h it is the same absolute step where nobody can
    read it. Over the ramp's own range - 0.1 to 100 mm/h, three decades - one
    byte gives steps of 2.8% *of the rate*, which is 0.0028 mm/h at the bottom.
    Still finer than the source, and far finer than a colour ramp can show.

    ``fields`` are packed into R, G and B in the order given; the caller decides
    what they mean and the manifest records it. Alpha is opaque everywhere, and
    that is load-bearing rather than lazy: a browser reading a frame back
    through a 2D canvas gets premultiplied pixels, so anything stored under
    alpha zero comes back as zeros. A band's lower edge lives exactly where the
    map is dry, so it has to sit on an opaque frame to survive being read.
    """
    fields = [np.asarray(field, dtype=np.float32) for field in fields]
    if len(fields) != 3:
        raise ValueError(f'a spread frame carries exactly three fields, got {len(fields)}')

    height, width = fields[0].shape
    rgba = np.zeros((height, width, 4), dtype=np.uint8)
    for channel, field in enumerate(fields):
        rgba[:, :, channel] = _to_log_byte(field, max_precip_mm_h)
    rgba[:, :, 3] = 255

    buffer = io.BytesIO()
    Image.fromarray(rgba, 'RGBA').save(
        buffer, format='WEBP', lossless=True, quality=100, method=4, exact=True
    )
    return buffer.getvalue()


def _to_log_byte(values, max_precip_mm_h: float):
    decades = np.log10(max_precip_mm_h / SPREAD_FLOOR_MM_H)
    clipped = np.clip(values, SPREAD_FLOOR_MM_H, max_precip_mm_h)
    scaled = 1 + np.rint(
        np.log10(clipped / SPREAD_FLOOR_MM_H) / decades * (_SPREAD_LEVELS - 1)
    )
    return np.where(values >= SPREAD_FLOOR_MM_H, scaled, 0).astype(np.uint8)


def decode_spread_frame(data: bytes, max_precip_mm_h: float):
    """Inverse of :func:`encode_spread_frame`, for tests and verification.

    Returns the three fields in the order they were packed. Zero comes back as
    zero rather than as the floor, so a dry pixel stays dry through the round
    trip instead of acquiring 0.1 mm/h it never had.
    """
    rgba = np.array(Image.open(io.BytesIO(data)).convert('RGBA'))
    decades = np.log10(max_precip_mm_h / SPREAD_FLOOR_MM_H)
    fields = []
    for channel in range(3):
        byte = rgba[:, :, channel].astype(np.float32)
        rate = SPREAD_FLOOR_MM_H * 10 ** ((byte - 1) / (_SPREAD_LEVELS - 1) * decades)
        fields.append(np.where(byte == 0, 0.0, rate).astype(np.float32))
    return fields
