"""Point forecasts with real uncertainty, for the homepage widget.

The map only ever needs one number per pixel, so the ingestor collapses the 20
ensemble members to a single field and discards the rest. A widget showing a
line with error bars needs the spread that was thrown away.

Rather than storing extra rasters, the members are sampled at a handful of
configured locations *while the timestep is already in memory* during the normal
ingest loop. That costs essentially nothing, and unlike sampling a quantised
frame afterwards it uses the exact member values.

Percentiles are taken at the location's own square kilometre, because that is
what a percentile of rain rate *here* means. Probability of rain is published
twice: once at that same cell, and once over a radius around it, which is a
different and usually more honest number - see :meth:`PointExtractor.observe`.

Only configured locations get this treatment. Arbitrary points would mean either
keeping every member on disk or decoding rasters per request; the map's
click-to-inspect popup already covers arbitrary points using the published
frames, which carry one reduced number per pixel and no spread at all.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime

import numpy as np

#: Percentiles published for each timestep. p10/p90 make the error bar, the
#: quartiles a tighter inner band, and p50 the line itself.
PERCENTILES = (10, 25, 50, 75, 90)

#: A member counts as "raining" at or above this rate, matching the bottom of
#: the colour ramp used on the map.
WET_THRESHOLD_MM_H = 0.1

#: Radius for the neighbourhood probability, in kilometres. See
#: :meth:`PointExtractor.observe` for why a point probability is not enough.
#: Ten is the usual choice for convective-scale ensembles and is roughly the
#: distance at which "it rained near me" stops meaning "it rained on me".
NEIGHBOURHOOD_KM = 10.0

#: Degrees of latitude per kilometre, near enough over a domain this size.
_KM_PER_DEGREE = 111.32


@dataclass(frozen=True)
class Location:
    name: str
    lat: float
    lon: float


def parse_locations(raw: str) -> list[Location]:
    """Parse ``name:lat:lon;name2:lat:lon`` into locations."""
    locations = []
    for chunk in raw.split(';'):
        chunk = chunk.strip()
        if not chunk:
            continue
        parts = chunk.split(':')
        if len(parts) != 3:
            raise ValueError(f'WIDGET_LOCATIONS entry must be name:lat:lon, got {chunk!r}')
        name, lat, lon = parts
        name = name.strip().lower()
        if not name.replace('-', '').replace('_', '').isalnum():
            raise ValueError(f'location name must be alphanumeric/-/_, got {name!r}')
        locations.append(Location(name=name, lat=float(lat), lon=float(lon)))

    names = [location.name for location in locations]
    if len(set(names)) != len(names):
        raise ValueError('WIDGET_LOCATIONS contains duplicate names')
    return locations


class PointExtractor:
    """Pulls every ensemble member's value at each location, timestep by timestep.

    The source grid is regular in lat/lon, so a location maps to one cell by
    plain arithmetic — no interpolation, which would blur an ensemble's spread.
    """

    def __init__(self, locations, lat, lon, neighbourhood_km: float = NEIGHBOURHOOD_KM,
                 crop_origin=(0, 0)):
        lat = np.asarray(lat, dtype=np.float64)
        lon = np.asarray(lon, dtype=np.float64)
        lat0, dlat = float(lat[0]), float(lat[1] - lat[0])
        lon0, dlon = float(lon[0]), float(lon[1] - lon[0])

        self.neighbourhood_km = float(neighbourhood_km)
        self.locations = []
        self._cells = []
        self._field_cells = []   # the same cells, offset into the published crop
        self._discs = []
        for location in locations:
            row = int(round((location.lat - lat0) / dlat))
            column = int(round((location.lon - lon0) / dlon))
            if not (0 <= row < len(lat) and 0 <= column < len(lon)):
                raise ValueError(
                    f'location {location.name!r} at {location.lat},{location.lon} '
                    'is outside the forecast domain'
                )
            self.locations.append(location)
            self._cells.append((row, column))
            self._field_cells.append((row - crop_origin[0], column - crop_origin[1]))
            self._discs.append(
                _disc(row, column, len(lat), len(lon), dlat, dlon,
                      location.lat, self.neighbourhood_km)
            )

        self._times: list[int] = []
        self._samples: list[np.ndarray] = []  # one (locations, members) array per step
        self._nearby: list[np.ndarray] = []   # one (locations,) array of NEP per step
        self._fields: list = []               # one (locations,) array of the drawn field
        self._nearby_bands: list = []         # one (locations, percentiles) per step
        self._estimated: list[bool] = []      # was this step stood in for?

    def __bool__(self):
        return bool(self.locations)

    def observe(self, valid_time: int, members, field=None, nearby=None,
                estimated: bool = False) -> None:
        """Record every member's value at each location for one timestep.

        Two probabilities come out of this, and the difference between them is
        the whole reason for the second. Counting the members that are wet on
        one square kilometre asks whether they agree on the *position* of a
        shower, which for anything convective they do not: twenty members can
        all forecast the same shower and each put it somewhere slightly
        different, and the count at your address comes out low for a shower
        nobody doubts. The neighbourhood count asks the question a reader
        actually means - how many members put rain anywhere near here - by
        letting each member be wet within a radius before counting it.

        ``field`` is the reduced field this timestep publishes - the same numbers
        the map draws, already cropped to what goes out. Taken before the frame
        format's ceiling, so above ``MAX_PRECIP_MM_H`` this carries the real
        rate where the frame saturates; the colour ramp's top reads "100+" for
        that reason and the two do not contradict each other. Sampled here because a
        reader comparing the two is entitled to the same answer from both, and
        the percentiles cannot supply it: they are the members at one square
        kilometre, while the field is a whole-domain reconstruction. The median
        of twenty members is dry unless half of them rain on that exact cell,
        so for showers it sits at zero through a cycle the map paints rain in.
        Publishing both is the only way to stop the chart and the map
        contradicting each other.

        ``nearby`` is the neighbourhood percentile stack the spread frames are
        built from - each member taken at its wettest within the published
        radius, then percentiles across members - on the uncropped grid, so it
        indexes the same way the members do. It exists here because neither of
        the other two numbers makes a usable line through time at one point.
        The members' own median is dry unless half of them rain on this exact
        cell; the field is dealt by *rank*, so it stays at zero until the cell
        climbs into the wettest few percent of the domain and then jumps, which
        put a chart's line and its band an hour out of step. Softening position
        into a small radius removes both problems at once.

        ``estimated`` says the members are not KNMI's for this timestep but a
        stand-in for a dead one, built from the steps either side of it - see
        :func:`ingestor.blend.repaired_steps`. It rides through to the published
        entry unchanged; the numbers are computed exactly as any other step's,
        because by this point they are ordinary member values and the only thing
        that differs is where they came from.
        """
        if not self.locations:
            return
        members = np.asarray(members)
        self._times.append(valid_time)
        self._estimated.append(bool(estimated))
        self._samples.append(
            np.stack([members[:, row, column] for row, column in self._cells])
        )
        self._nearby.append(np.array([
            float((members[:, rows, columns] >= WET_THRESHOLD_MM_H).any(axis=1).mean())
            for rows, columns in self._discs
        ]))
        # None rather than zero for a location outside the published crop: it
        # has no drawn field, which is not the same as a dry one.
        self._fields.append(None if field is None else [
            float(field[row, column])
            if 0 <= row < field.shape[0] and 0 <= column < field.shape[1] else None
            for row, column in self._field_cells
        ])
        # Uncropped, so these read at the location's own cell rather than the
        # crop-relative one the field uses.
        self._nearby_bands.append(None if nearby is None else [
            [float(percentile[row, column]) for percentile in nearby]
            for row, column in self._cells
        ])

    def series_for(self, index: int) -> list[dict]:
        """The published series for one location, one entry per timestep."""
        entries = []
        for valid_time, sample, nearby, drawn, band, estimated in zip(
                self._times, self._samples, self._nearby, self._fields,
                self._nearby_bands, self._estimated):
            values = np.asarray(sample[index], dtype=np.float64)
            quantiles = np.percentile(values, PERCENTILES)
            entry = {'t': int(valid_time)}
            for percentile, value in zip(PERCENTILES, quantiles):
                key = 'median' if percentile == 50 else f'p{percentile}'
                entry[key] = _round(value)
            entry['mean'] = _round(values.mean())
            # What the ensemble is really for: how many members say it rains.
            entry['probability'] = round(float((values >= WET_THRESHOLD_MM_H).mean()), 2)
            entry['probability_nearby'] = round(float(nearby[index]), 2)
            # What the map draws here, under its own name. Not `median`: it is a
            # probability-matched mean by default and can be configured to be a
            # mean or a maximum, none of which is the median sitting beside it.
            if drawn is not None and drawn[index] is not None:
                entry['field'] = _round(drawn[index])
            # The same three the spread frames carry, at this one location.
            if band is not None:
                low, middle, high = band[index]
                entry['nearby_p10'] = _round(low)
                entry['nearby_median'] = _round(middle)
                entry['nearby_p90'] = _round(high)
            # Only when true, so a reader that predates the flag is unaffected.
            if estimated:
                entry['estimated'] = True
            entries.append(entry)
        return entries


def _disc(row, column, rows, columns, dlat, dlon, latitude, radius_km):
    """Flat index arrays for the cells within ``radius_km`` of one cell.

    Returned pre-flattened so a member's neighbourhood is one fancy-index gather
    rather than a slice plus a mask, which is what keeps this affordable inside
    the ingest loop: it runs once per location per timestep, 72 times a cycle.
    """
    # Longitude degrees are shorter than latitude ones, by more at 53 N than at
    # 50 N. Getting this wrong would stretch the disc into an ellipse.
    row_reach = max(1, int(math.ceil(radius_km / (abs(dlat) * _KM_PER_DEGREE))))
    lon_km = abs(dlon) * _KM_PER_DEGREE * math.cos(math.radians(latitude))
    column_reach = max(1, int(math.ceil(radius_km / lon_km)))

    offsets_y = np.arange(-row_reach, row_reach + 1)
    offsets_x = np.arange(-column_reach, column_reach + 1)
    grid_y, grid_x = np.meshgrid(offsets_y, offsets_x, indexing='ij')

    distance_km = np.hypot(
        grid_y * abs(dlat) * _KM_PER_DEGREE,
        grid_x * lon_km,
    )
    inside = distance_km <= radius_km

    absolute_y = row + grid_y
    absolute_x = column + grid_x
    # Clipped at the domain edge rather than wrapped, so a location near the
    # border counts the cells that exist instead of cells on the far side.
    inside &= (
        (absolute_y >= 0) & (absolute_y < rows)
        & (absolute_x >= 0) & (absolute_x < columns)
    )
    return absolute_y[inside], absolute_x[inside]


def _round(value: float) -> float:
    # Two decimals is finer than the source's own 0.01 mm/h resolution.
    return round(float(value), 2)


def summarise(series: list[dict], reference_time: int,
              radius_km: float | None = None,
              max_mm_h: float | None = None) -> dict:
    """A plain-language read of the series, for a widget with one line of room.

    Describes the same number the map paints and the chart draws - each entry's
    ``field`` - rather than the median of the members at this square kilometre.
    They are different estimators and for showers they disagree completely: the
    median is dry unless half the members rain on that exact cell, so a sentence
    built on it said "dry now" over a chart peaking at 141 mm/h. Falling back to
    the median keeps a series published before ``field`` existed readable.

    Describes the *spell* — the contiguous run of wet steps — rather than the
    moment it begins. The onset rate is by construction the instant the field
    crosses the wet threshold, so it is always a small number whether what
    follows is five minutes of drizzle or the leading edge of a downpour; the
    peak, and how far ahead of the onset it sits, is what tells them apart.

    The peak is taken from the current spell only. A second shower three hours
    out is a different event and must not be quoted as this one's severity.

    The structured fields carry the same facts as ``text``, so a caller with
    less room than a sentence can compose its own line instead of parsing this
    one.

    ``radius_km`` is only used to name the neighbourhood in the sentence; with
    it left out the text says "nearby" instead.

    ``max_mm_h`` is the frame format's ceiling, and the sentence stops there
    because the map does. The field is published unclipped, so at the domain's
    wettest ranked cell it can read 481 mm/h where the frames saturate at 100
    and the colour ramp's top says "100+" — a rate no reader can check against
    anything on screen, and one that is only that large because probability
    matching hands the top rank the wettest value any member produced anywhere.
    The prose says "over 100 mm/h" there; ``peak_mm_h`` keeps the real number,
    because a structured field has no ramp to disagree with.
    """
    future = [entry for entry in series if entry['t'] >= reference_time]
    if not future:
        return {'raining_now': False, 'text': 'No forecast available.'}

    onset_index = next(
        (i for i, entry in enumerate(future) if _drawn(entry) >= WET_THRESHOLD_MM_H),
        None,
    )

    if onset_index is None:
        # A dry field still leaves room for the ensemble to be hinting at rain,
        # and this is what the neighbourhood probability was added for. The
        # field being dry says the map draws nothing on this cell; it says
        # nothing about the showers the members are putting a few kilometres
        # away, which for anything convective is where the disagreement lives.
        best_chance = max(future, key=_nearby_probability)
        chance = _nearby_probability(best_chance)
        if chance >= 0.3:
            # Two sentences, not one with a percentage swapped in. A dry cell
            # and a near-certain neighbourhood are not a contradiction - it is
            # what "showers about, one of them may be yours" looks like in
            # numbers - but "probably dry, 100% chance of rain" reads as a bug.
            where = f'within {round(radius_km)} km' if radius_km else 'nearby'
            when = _clock(best_chance['t'])
            text = (
                f'Showers about — {round(chance * 100)}% of members put rain '
                f'{where} around {when}, though it may miss you.'
                if chance >= 0.7 else
                f'Probably dry, but a {round(chance * 100)}% chance of a shower '
                f'{where} around {when}.'
            )
            return {'raining_now': False, 'starts_at': None,
                    'chance_nearby': round(chance, 2), 'chance_at': best_chance['t'],
                    'text': text}
        return {'raining_now': False, 'starts_at': None, 'text': 'Staying dry.'}

    dry_after = next(
        (i for i in range(onset_index + 1, len(future))
         if _drawn(future[i]) < WET_THRESHOLD_MM_H),
        None,
    )
    spell = future[onset_index:dry_after]
    clearing = future[dry_after] if dry_after is not None else None

    onset = spell[0]
    peak = max(spell, key=_drawn)
    raining_now = onset_index == 0

    # Only worth its own clause if it is meaningfully heavier than the start,
    # otherwise it is the same number twice.
    peak_matters = (
        peak['t'] != onset['t']
        and _drawn(peak) >= _drawn(onset) * 1.5 + 0.05
    )

    lead_minutes = round((onset['t'] - reference_time) / 60)
    # Whether the sentence counts minutes from now or names clock times. Mixing
    # the two makes "rain at 11:25, up to 4 mm/h within 30 min" ambiguous:
    # thirty minutes from now, or from an onset two hours away?
    relative = raining_now or lead_minutes < 90

    if raining_now:
        parts = [f'Raining now at {_capped(_drawn(onset), max_mm_h)} mm/h']
    else:
        when = f'in {lead_minutes} min' if relative else f'at {_clock(onset["t"])}'
        parts = [f'Dry now — rain {when}' if peak_matters
                 else f'Dry now — rain {when} at {_capped(_drawn(onset), max_mm_h)} mm/h']

    if peak_matters:
        climb = round((peak['t'] - onset['t']) / 60)
        parts.append(
            f'up to {_capped(_drawn(peak), max_mm_h)} mm/h within {climb} min'
            if relative and climb <= 30
            else f'peaking {_capped(_drawn(peak), max_mm_h)} mm/h around {_clock(peak["t"])}'
        )

    parts.append(
        f'easing off around {_clock(clearing["t"])}' if clearing
        else 'lasting past the end of the forecast'
    )

    return {
        'raining_now': raining_now,
        'starts_at': None if raining_now else onset['t'],
        'stops_at': clearing['t'] if clearing else None,
        'peak_mm_h': _drawn(peak),
        'peak_at': peak['t'],
        'text': ', '.join(parts) + '.',
    }


def _drawn(entry: dict) -> float:
    """The number this entry is drawn as, best available first.

    The neighbourhood median, then the field, then the members' own median.
    That order is the order of usefulness at a single location over time, and
    each step down is a fallback for a document written before the key above it
    existed - the same reason :func:`_nearby_probability` has one.

    The neighbourhood median leads because the other two are hard to read as a
    line. The members' median is dry unless half of them rain on this exact
    square kilometre. The field is dealt by rank, so at one cell it sits at zero
    until that cell climbs into the wettest few percent of the domain and then
    jumps - which had a chart's line peaking half an hour after the band around
    it. Softening position into a small radius removes both.
    """
    for key in ('nearby_median', 'field'):
        value = entry.get(key)
        if value is not None:
            return float(value)
    return float(entry.get('median', 0.0))


def _nearby_probability(entry: dict) -> float:
    """The neighbourhood probability, falling back to the point one.

    The fallback is for documents written before the neighbourhood count
    existed, which a running container can still be holding.
    """
    value = entry.get('probability_nearby')
    return float(entry.get('probability', 0.0) if value is None else value)


def _capped(mmh: float, ceiling: float | None) -> str:
    """A rate as the map can show it, saying "over" where the ramp saturates."""
    if ceiling and mmh > ceiling:
        return f'over {_rate(ceiling)}'
    return _rate(mmh)


def _rate(mmh: float) -> str:
    """Match the frontend's rate formatting, so the two never disagree."""
    if mmh >= 10:
        return str(round(mmh))
    text = f'{mmh:.1f}' if mmh >= 1 else f'{mmh:.2f}'
    return text.rstrip('0').rstrip('.')


def _clock(timestamp: int) -> str:
    """Local 24-hour time.

    Was UTC, with the offset spelled out in the string — which was honest but
    unhelpful: a reader in Amsterdam saw "12:35 UTC" for something happening at
    half past two. Now that the container sets TZ (it has to, or the alert
    bodies name the wrong hour), local time is both correct and what the rest
    of the app shows.
    """
    return datetime.fromtimestamp(timestamp).strftime('%H:%M')


def _value_at(series, when: int):
    """The entry nearest ``when``, or None for an empty series."""
    if not series:
        return None
    return min(series, key=lambda entry: abs(entry['t'] - when))


def current_conditions(document: dict) -> dict:
    """A compact "right now" view of a point forecast.

    A homepage widget that only wants a headline should not have to pull down
    and reduce ~80 timesteps to find it, so the reduction happens here, once
    per cycle, rather than in every client.
    """
    reference = document['reference_time']
    now = {
        'generated_at': document['generated_at'],
        'reference_time': reference,
        'location': document['location'],
        'summary': document['summary'],
    }

    rain = _value_at(document['precipitation']['series'], reference)
    if rain:
        now['precipitation'] = {
            'unit': document['precipitation']['unit'],
            # The same number the map paints and the summary describes, so a
            # dashboard headline cannot disagree with the page it links to.
            # `median` rides along beside it because they are different
            # questions, not two spellings of one.
            'value': _drawn(rain),
            'product': document['precipitation'].get('field_product'),
            'median': rain['median'],
            'p10': rain['p10'],
            'p90': rain['p90'],
            'probability': rain['probability'],
            'probability_nearby': _nearby_probability(rain),
        }
        # A widget with one line of room still should not present a stand-in
        # for a dead timestep as a measurement of the present.
        if rain.get('estimated'):
            now['precipitation']['estimated'] = True

    for key in ('temperature', 'wind', 'solar'):
        block = document.get(key)
        entry = _value_at(block['series'], reference) if block else None
        if entry:
            now[key] = {
                'unit': block['unit'],
                'value': entry['median'],
                'p10': entry['p10'],
                'p90': entry['p90'],
            }

    return now
