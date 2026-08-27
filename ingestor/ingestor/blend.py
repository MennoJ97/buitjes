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

Not every timestep KNMI publishes is usable. Roughly once a cycle the blend
emits a dead one, and reading it literally puts a hole in the middle of a
shower; :func:`repaired_steps` is the loop that notices and stands in for it.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone

import h5py
import numpy as np

log = logging.getLogger('ingestor.blend')

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


# --------------------------------------------------------------- dead steps


def is_degenerate(members) -> bool:
    """Whether a timestep carries no ensemble at all.

    KNMI's blend publishes a dead timestep roughly once per cycle, somewhere
    around the three-hour lead — the index moves between cycles, so it is not a
    fixed step going wrong. What it looks like in the file, taking the 06:25Z
    cycle of 27 August 2026 as the example, is step 36 (+185 min):

    ::

        step       wet cells per member    peak mm/h per member
        35 (+180)  ~30,500, all different  29.6 … 35.5, varying
        36 (+185)  173, all identical      1.00, all identical
        37 (+190)  ~30,200, all different  29.7 … 35.6, varying

    All twenty members byte-identical, the field empty but for 173 cells on the
    southernmost grid row each holding exactly 1.00 mm/h. That is a placeholder,
    not a forecast, and taken at face value it reads as five minutes of
    nationwide dry in the middle of a rain band.

    Identical members is the test, because it is the one thing a real ensemble
    cannot be. Twenty members of a convective blend agreeing to the bit over
    600,000 grid points does not happen; the only lawful way it could is a
    domain — half of western Europe, here — with no precipitation anywhere at
    all, and in that case the steps either side are dry too and standing in for
    this one changes nothing but the label.

    Deliberately narrow. A looser test (a collapse in wet area, say) would catch
    more shapes of corruption, and would also eventually fire on real weather,
    which is the worse failure: inventing rain over a genuine lull is a bigger
    lie than the one being fixed. Every hit is logged, so a variant this misses
    should show up as an unexplained blip rather than stay invisible.
    """
    members = np.asarray(members)
    if len(members) < 2:
        return False  # nothing to disagree with; a one-member product is not this
    first = members[0]
    return all(np.array_equal(members[i], first) for i in range(1, len(members)))


def estimate_step(before, after):
    """Stand in for a dead timestep with the members either side of it.

    Member-wise rather than pooled: member 7 at +185 min is the average of
    member 7 at +180 and +190, so each member keeps its own field and the
    ensemble keeps its spread. Averaging across members instead would hand every
    one of them the same field and reproduce, in a subtler form, exactly the
    defect being repaired.

    Five minutes either side is a short enough hop that the field barely moves.
    It does smear a fast shower over the two positions it held, which is a real
    cost and the reason the step is published flagged rather than silently.

    With only one neighbour — the step is first or last, or its other side is
    dead too — that neighbour is used alone, which is persistence over five
    minutes. Returned as-is rather than copied; nothing downstream writes to a
    member array. ``None`` when there is nothing on either side, which is the
    caller's cue that the step is genuinely unavailable.
    """
    if before is None:
        return after
    if after is None:
        return before
    return (before + after) * np.float32(0.5)


def repaired_steps(source):
    """Yield ``(valid_time, members, estimated)`` for every usable timestep.

    ``estimated`` marks a step that :func:`is_degenerate` rejected and
    :func:`estimate_step` stood in for. A step that could not be stood in for is
    not yielded at all: a gap in the timeline is the honest answer, and every
    consumer of the manifest and the point series already copes with the frame
    list being any subset of the cadence.

    Only immediately adjacent steps are blended from, so nothing is ever
    estimated across more than five minutes. Two dead steps in a row therefore
    lean outward, one to each side, rather than both reaching for the same
    distant survivor.

    Reads each step once. The step read ahead to repair its predecessor is
    carried into the next iteration rather than fetched again — the read is
    ~24 MiB off disk and an HDF5 decompression, which is the expensive part of a
    cycle. Peak memory is three member arrays during a repair (the two sides and
    the average) against one in the normal case; see the ingestor's `mem_limit`.
    """
    count = len(source.valid_times)
    pending = None    # already read while looking ahead from the previous step
    previous = None   # the step just before this one, and only if it was sound

    for index, valid_time in enumerate(source.valid_times):
        members = pending if pending is not None else source.members(index)
        pending = None

        if not is_degenerate(members):
            previous = members
            yield valid_time, members, False
            continue

        # Drop the dead array before reading its neighbour, so the repair costs
        # one extra step in memory rather than two.
        del members
        following = None
        if index + 1 < count:
            pending = source.members(index + 1)
            if not is_degenerate(pending):
                following = pending

        sides = [name for name, side in (('before', previous), ('after', following))
                 if side is not None]
        repaired = estimate_step(previous, following)
        previous = None
        if repaired is None:
            log.warning(
                'step %d (%d) carries no ensemble and neither neighbour can '
                'stand in for it; publishing a gap', index, valid_time,
            )
            continue

        log.warning('step %d (%d) carries no ensemble; estimated from the step %s it',
                    index, valid_time, ' and '.join(sides))
        yield valid_time, repaired, True
