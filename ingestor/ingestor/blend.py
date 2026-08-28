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
import math
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
    points = ranks.size

    # Only the wet values are sorted, which is bit-identical to sorting the
    # pool and about 20% quicker. The pool is every member value in descending
    # order, and on this product nineteen in twenty of them are zero - sorting
    # those is sorting a known constant. Dropped, the ranks past the wet ones
    # are left holding blocks that are entirely zero, which is what `blocks`
    # already holds. Rain rate is non-negative, so `> 0` really does split the
    # pool at the point the zeros begin.
    wet = np.sort(values[values > 0])[::-1]
    blocks = np.zeros(points, dtype=np.float32)
    whole = wet.size // members
    if whole:
        blocks[:whole] = wet[:whole * members].reshape(whole, members).mean(axis=1)
    if wet.size > whole * members:
        # The single rank whose block straddles the wet/dry boundary. The zeros
        # are written back out for it, rather than divided in as a count: a
        # float32 `sum() / members` accumulates in a different order from the
        # `mean()` above and left this one rank a few times 1e-9 off, which is
        # nothing on a rain rate but is the difference between "identical" and
        # "identical apart from one pixel" in the test that guards this.
        straddling = np.zeros(members, dtype=np.float32)
        tail = wet[whole * members:]
        straddling[:tail.size] = tail
        blocks[whole] = straddling.mean()

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


# ------------------------------------------------------------------- spread

#: Degrees of latitude per kilometre, near enough over a domain this size.
#: :mod:`ingestor.points` does the same arithmetic for its disc; the shapes
#: differ on purpose - see :func:`cell_reach`.
_KM_PER_DEGREE = 111.32

#: What the spread frames carry, low to high. The median rides along because a
#: band without a middle cannot be compared with the field it describes.
SPREAD_PERCENTILES = (10, 50, 90)


def cell_reach(radius_km: float, dlat: float, dlon: float, latitude: float):
    """``(rows, columns)`` spanning ``radius_km`` from a cell, as a rectangle.

    A rectangle rather than the disc :mod:`ingestor.points` uses, because this
    one is applied to every pixel rather than to a handful of locations, and a
    rectangle is separable: a running maximum along the rows and then along the
    columns costs O(log r) passes over the array, where a disc costs O(r^2)
    gathers. The corners reach r*sqrt(2) instead of r, which for a band that is
    already a statement about "somewhere near here" is a rounding error on a
    radius chosen by taste.

    Longitude degrees are shorter than latitude ones, by more at 53 N than at
    50 N; without the cosine the rectangle would stretch east-west.
    """
    if radius_km <= 0:
        return 0, 0
    rows = int(math.ceil(radius_km / (abs(dlat) * _KM_PER_DEGREE)))
    lon_km = abs(dlon) * _KM_PER_DEGREE * math.cos(math.radians(latitude))
    return rows, int(math.ceil(radius_km / lon_km))


def _shift_into(target, values, offset: int, axis: int):
    """Write ``values``, moved along ``axis``, into ``target``.

    Into a caller-owned buffer rather than a fresh array, and only the vacated
    edge is cleared rather than the whole thing. Allocating and zero-filling a
    48 MiB member stack eight times per timestep, purely to blank memory about
    to be overwritten, was half the cost of the dilation.
    """
    filled = [slice(None)] * values.ndim
    source = [slice(None)] * values.ndim
    vacated = [slice(None)] * values.ndim
    if offset > 0:
        filled[axis] = slice(offset, None)
        source[axis] = slice(None, -offset)
        vacated[axis] = slice(None, offset)
    else:
        filled[axis] = slice(None, offset)
        source[axis] = slice(-offset, None)
        vacated[axis] = slice(offset, None)
    target[tuple(filled)] = values[tuple(source)]
    target[tuple(vacated)] = 0


def neighbourhood_maximum(values, reach: int, axis: int):
    """Running maximum over +-``reach`` cells along one axis.

    Doubling, not a sliding window: maximum over +-1, then that result over
    +-2, then +-4, and each pass doubles the span already covered. Ten cells
    take four passes instead of twenty-one comparisons, and the cost grows with
    the logarithm of the radius rather than with the radius, which is what makes
    a wider neighbourhood free to try.

    Zeros vacate at the domain edge, which is the identity for a maximum over
    rain rates - a cell near the border takes the maximum of the cells that
    exist, exactly as the point forecasts' disc clips rather than wraps.

    The two directions are folded in one at a time, in place. Written the
    obvious way - ``maximum(maximum(out, shift(+s)), shift(-s))`` - a pass holds
    five copies of a 48 MiB member stack at once, which on this container is the
    difference between fitting and being killed. One at a time is also correct:
    after the forward pass a cell already holds the maximum over ``[i-s, i]``,
    so the backward pass over that spans ``[i-s, i+s]``.
    """
    out = np.array(values, dtype=np.float32, copy=True)
    if reach <= 0:
        return out
    return _dilate(out, reach, axis, np.empty_like(out))


def _dilate(out, reach: int, axis: int, scratch):
    """:func:`neighbourhood_maximum` in place, over a buffer it is handed.

    Separate so a caller doing both axes pays for one copy and one scratch
    rather than two of each.
    """
    covered, span = 0, 1
    while covered < reach:
        step = min(span, reach - covered)
        for offset in (step, -step):
            _shift_into(scratch, out, offset, axis)
            np.maximum(out, scratch, out=out)
        covered += step
        span *= 2
    return out


def spread_fields(members, reach_rows: int, reach_columns: int,
                  percentiles=SPREAD_PERCENTILES):
    """Percentiles of what each member puts *within reach* of every pixel.

    The map draws the probability-matched mean, which is a statement about the
    field: the pooled intensities of the whole ensemble, placed by the mean's
    spatial ranking. Percentiles taken at a single cell are a statement about
    that cell, and for showers they are dominated by the members disagreeing
    about *where* rather than *whether* - which is why the point forecasts'
    median sits at zero through a cycle the map paints rain in. A band built
    that way brackets a value it is not describing.

    Taking each member's maximum over a small neighbourhood first collapses the
    position disagreement into a spatial tolerance, so what comes out is "how
    hard could it rain within r of here, according to each member" - a field
    statement, like the one it accompanies. The radius is the whole trade:
    wider lifts the band's floor off dry but eventually lifts it past the field
    it describes. Measured over one cycle, the share of painted pixels whose
    value falls inside the band, against the share whose p25 is pinned at dry:

    ::

        radius   pmm inside band   p25 pinned dry   band width
        per-cell          94.0%            60.2%     3.08 mm/h
        3 km              90.6%            42.6%     4.86 mm/h
        10 km             82.6%            25.2%     7.49 mm/h
        20 km             66.3%            13.0%    10.99 mm/h

    Returned in the order given, so the caller can pack them without guessing.
    """
    near = np.array(members, dtype=np.float32, copy=True)
    scratch = np.empty_like(near)
    _dilate(near, reach_rows, 1, scratch)
    _dilate(near, reach_columns, 2, scratch)
    del scratch          # 48 MiB back before the sort asks for its own
    return _percentiles_of_sorted(near, percentiles)


def _percentiles_of_sorted(stack, percentiles):
    """Percentiles down axis 0, by sorting once instead of partitioning N times.

    ``np.percentile`` runs an independent partition per quantile, which for
    three quantiles over twenty members costs three passes where one sort would
    do. Sorting twenty values per column and reading off the interpolated
    positions is 3.1x faster on a full member stack, and matches numpy to 2e-6
    mm/h - float32 rounding in the interpolation, against a channel whose steps
    are 2.8% of the rate and source data quantised at 0.01 mm/h.

    Sorts ``stack`` in place: it is the dilated copy, which nothing else holds.
    Positions come from the member count rather than being written down for
    twenty, so a product that publishes a different ensemble size still gets
    the percentiles it asked for.
    """
    stack.sort(axis=0)
    count = stack.shape[0]
    fields = []
    for percentile in percentiles:
        position = percentile / 100 * (count - 1)
        lower = int(math.floor(position))
        upper = min(lower + 1, count - 1)
        weight = position - lower
        fields.append(stack[lower] if weight == 0 else
                      stack[lower] * (1 - weight) + stack[upper] * weight)
    return np.stack(fields).astype(np.float32)
