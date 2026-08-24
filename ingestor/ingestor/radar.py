"""Reading KNMI's real-time gauge-corrected radar composite (RTCOR).

Dataset ``nl_rdr_data_rtcor_5m``: one KNMI HDF5 file every 5 minutes, ~76 KiB,
holding a 765x700 1 km composite from eight Dutch, Belgian and German radars.

Two things differ from the forecast product:

* The grid is **polar stereographic**, not lat/lon, so it needs real
  reprojection (see :class:`~ingestor.raster.StereographicResampler`).
* ``image1`` is a 5-minute precipitation *accumulation* in mm, not a rate.
  Multiply by 12 to get mm/h, which is what every other part of the pipeline
  speaks.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone

import h5py
import numpy as np

#: RAD_NL25_RAC_RT_<YYYYMMDDHHMM>.h5 - the stamp is the accumulation's end time.
FILENAME_PATTERN = re.compile(r'^RAD_NL25_RAC_RT_(\d{12})\.h5$')

#: A 5-minute accumulation in mm is this many mm/h.
ACCUMULATION_TO_RATE = 12.0


def valid_time_from_filename(filename: str) -> int | None:
    match = FILENAME_PATTERN.match(filename)
    if not match:
        return None
    stamp = datetime.strptime(match.group(1), '%Y%m%d%H%M').replace(tzinfo=timezone.utc)
    return int(stamp.timestamp())


def _text(value) -> str:
    return value.decode() if isinstance(value, bytes) else str(value)


class RadarFile:
    """One RTCOR composite. Use as a context manager."""

    def __init__(self, path: str):
        self._file = h5py.File(path, 'r')
        geographic = self._file['geographic']
        self.proj4 = _text(geographic['map_projection'].attrs['projection_proj4_params'])
        self.columns = int(geographic.attrs['geo_number_columns'][0])
        self.rows = int(geographic.attrs['geo_number_rows'][0])
        self.corners = np.asarray(geographic.attrs['geo_product_corners'], dtype=np.float64)
        self.pixel_size = (
            float(geographic.attrs['geo_pixel_size_x'][0]),
            float(geographic.attrs['geo_pixel_size_y'][0]),
        )

        calibration = self._file['image1/calibration'].attrs
        formula = _text(calibration['calibration_formulas'])
        self._scale, self._offset = _parse_calibration(formula)
        self._missing = int(np.ravel(calibration['calibration_missing_data'])[0])
        self._out_of_image = int(np.ravel(calibration['calibration_out_of_image'])[0])

        end = _text(self._file['overview'].attrs['product_datetime_end'])
        self.valid_time = _parse_knmi_datetime(end)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self._file.close()

    def rate_and_validity(self):
        """Rain rate in mm/h plus a mask of pixels the radar actually measured."""
        raw = self._file['image1/image_data'][:]
        valid = (raw != self._missing) & (raw != self._out_of_image)
        rate = (raw.astype(np.float32) * self._scale + self._offset) * ACCUMULATION_TO_RATE
        return np.where(valid, rate, 0.0), valid


def _parse_calibration(formula: str):
    """Parse 'GEO=0.010000*PV+0.000000' into (scale, offset)."""
    match = re.match(r'\s*GEO\s*=\s*([-\d.eE+]+)\s*\*\s*PV\s*([-+]\s*[\d.eE+-]+)\s*$', formula)
    if not match:
        raise ValueError(f'unrecognised calibration formula: {formula!r}')
    return float(match.group(1)), float(match.group(2).replace(' ', ''))


def _parse_knmi_datetime(text: str) -> int:
    """Parse '24-AUG-2026;15:45:00.000' (UTC) into a unix timestamp."""
    stamp = datetime.strptime(text.strip().split('.')[0], '%d-%b-%Y;%H:%M:%S')
    return int(stamp.replace(tzinfo=timezone.utc).timestamp())
