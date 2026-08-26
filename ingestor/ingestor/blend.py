"""Reading KNMI's seamless precipitation ensemble forecast.

The product is pySTEPS blending radar extrapolation with the HARMONIE-AROME
ensemble, published every 5 minutes as one NetCDF4 file holding
``precip_intensity`` with shape (member, time, lat, lon) - 20 members, 72 steps
from +5 minutes to +6 hours, on a regular 1 km lat/lon grid.

Members are reduced to a single field per timestep here. The reduction is not a
detail: a pixel-wise median or mean of members that disagree about *where* a
shower will be is not a field any member forecast, and it is systematically too
dry and too flat. See :func:`probability_matched_mean`.

The point forecasts keep the members themselves rather than a reduction, which
is what makes real spread available; see :mod:`ingestor.points`.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone

import h5py
import numpy as np

_UNITS_PATTERN = re.compile(
    r'seconds since (\d{4})-(\d{2})-(\d{2})[ T](\d{2}):(\d{2}):(\d{2})'
)

def probability_matched_mean(values):
    """Ebert's probability-matched mean of a ``(member, y, x)`` stack.

    The ensemble mean gets the *placement* right — averaging is what cancels
    each member's individual displacement error — and the intensities wrong,
    because averaging fields whose showers sit in different places spreads one
    shower's water over all of their footprints. The result is too wide, too
    flat, and peaks at a fraction of what any member forecast. The pixel-wise
    median is worse still: it is dry wherever fewer than half the members put
    rain on that exact square kilometre, so it does not conserve the ensemble's
    water at all, and the loss grows with lead time as the members diverge.

    Probability matching keeps the mean's spatial pattern and throws its
    intensities away. Rank every grid point by the ensemble mean, rank every
    value any member forecast anywhere in the domain, then hand the nth-wettest
    grid point the nth-wettest block of member values. Wet area and intensity
    distribution come out equal to a single member's, while *where* the rain is
    remains the mean's answer rather than any one member's guess.

    Each rank takes the *average* of its block of pooled values rather than one
    representative of it. Ebert's original subsamples every nth value; averaging
    the block uses all of them, which costs nothing and stops the very top rank
    from being handed the single wildest pixel any member produced.

    Ties cost nothing here. The mean is exactly zero only where every member is
    dry, and those points hold exactly enough of the pool's zeros between them,
    so no rain can be dealt to a point the whole ensemble called dry.
    """
    values = np.asarray(values, dtype=np.float32)
    members = values.shape[0]
    mean = values.mean(axis=0)

    ranks = np.argsort(mean, axis=None)[::-1]
    pooled = np.sort(values, axis=None)[::-1]

    points = ranks.size
    blocks = pooled[:points * members].reshape(points, members).mean(axis=1)

    matched = np.empty(points, dtype=np.float32)
    matched[ranks] = blocks
    return matched.reshape(mean.shape)


_REDUCERS = {
    'pmm': probability_matched_mean,
    'median': lambda a: np.median(a, axis=0),
    'mean': lambda a: np.mean(a, axis=0),
    'max': lambda a: np.max(a, axis=0),
}

#: How each reduction should be described in the manifest, for a reader who has
#: to know what the numbers on the map actually are.
REDUCER_LABELS = {
    'pmm': 'probability-matched mean',
    'median': 'median',
    'mean': 'mean',
    'max': 'maximum',
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

    def field(self, index: int, stat: str = 'pmm'):
        """Rain rate in mm/h at one timestep, reduced across ensemble members."""
        return reduce_members(self.members(index), stat)


def reduce_members(values, stat: str = 'pmm'):
    """Collapse a ``(member, y, x)`` array to a single field.

    Pass the members already cropped to what will be published. ``pmm`` pools
    values across the whole array it is given, so handing it the full KNMI
    domain would let a downpour over Germany set the intensities shown over the
    Randstad.
    """
    return _REDUCERS[stat](values)
