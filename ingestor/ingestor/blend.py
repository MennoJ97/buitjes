"""Reading KNMI's seamless precipitation ensemble forecast.

The product is pySTEPS blending radar extrapolation with the HARMONIE-AROME
ensemble, published every 5 minutes as one NetCDF4 file holding
``precip_intensity`` with shape (member, time, lat, lon) - 20 members, 72 steps
from +5 minutes to +6 hours, on a regular 1 km lat/lon grid.

Members are reduced to a single field per timestep here. Keeping all 20 is what
would unlock probability and spread products later; see the ensemble notes in
docs/IMPLEMENTATION_PLAN.md.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone

import h5py
import numpy as np

_UNITS_PATTERN = re.compile(
    r'seconds since (\d{4})-(\d{2})-(\d{2})[ T](\d{2}):(\d{2}):(\d{2})'
)

_REDUCERS = {
    'median': lambda a: np.median(a, axis=0),
    'mean': lambda a: np.mean(a, axis=0),
    'max': lambda a: np.max(a, axis=0),
}


def _text(value) -> str:
    return value.decode() if isinstance(value, bytes) else str(value)


def _epoch_from_units(units) -> int:
    match = _UNITS_PATTERN.match(_text(units))
    if not match:
        raise ValueError(f'unrecognised time units: {_text(units)!r}')
    year, month, day, hour, minute, second = (int(g) for g in match.groups())
    stamp = datetime(year, month, day, hour, minute, second, tzinfo=timezone.utc)
    return int(stamp.timestamp())


class BlendFile:
    """One published cycle. Use as a context manager."""

    def __init__(self, path: str):
        self._file = h5py.File(path, 'r')
        variable = self._file['precip_intensity']
        self._variable = variable
        self.lat = self._file['lat'][:]
        self.lon = self._file['lon'][:]

        offsets = self._file['time'][:]
        self.reference_time = _epoch_from_units(self._file['time'].attrs['units'])
        self.valid_times = [self.reference_time + int(o) for o in offsets]

        self._scale = float(np.ravel(variable.attrs.get('scale_factor', 1.0))[0])
        self._offset = float(np.ravel(variable.attrs.get('add_offset', 0.0))[0])
        fill = variable.attrs.get('_FillValue')
        self._fill = None if fill is None else np.ravel(fill)[0]
        self.member_count = variable.shape[0]

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self._file.close()

    def __len__(self):
        return len(self.valid_times)

    def members(self, index: int):
        """Every member's rain rate in mm/h at one timestep, as (member, lat, lon).

        This is the expensive read (~24 MiB), so callers that need both the map
        field and point samples should take this once and derive both from it.
        """
        raw = self._variable[:, index, :, :]
        if self._fill is not None:
            raw = np.where(raw == self._fill, 0, raw)
        return raw.astype(np.float32) * self._scale + self._offset

    def field(self, index: int, stat: str = 'median'):
        """Rain rate in mm/h at one timestep, reduced across ensemble members."""
        return reduce_members(self.members(index), stat)


def reduce_members(values, stat: str = 'median'):
    """Collapse a (member, ...) array to a single field."""
    return _REDUCERS[stat](values)
