"""Point forecasts with real uncertainty, for the homepage widget.

The map only ever needs one number per pixel, so the ingestor collapses the 20
ensemble members to a median and discards the rest. A widget showing a line with
error bars needs the spread that was thrown away.

Rather than storing extra rasters, the members are sampled at a handful of
configured locations *while the timestep is already in memory* during the normal
ingest loop. That costs essentially nothing, and unlike sampling a quantised
frame afterwards it uses the exact member values.

Only configured locations get this treatment. Arbitrary points would mean either
keeping every member on disk or decoding rasters per request; the map's
click-to-inspect popup already covers arbitrary points using the median frames.
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

    def __init__(self, locations, lat, lon):
        lat = np.asarray(lat, dtype=np.float64)
        lon = np.asarray(lon, dtype=np.float64)
        lat0, dlat = float(lat[0]), float(lat[1] - lat[0])
        lon0, dlon = float(lon[0]), float(lon[1] - lon[0])

        self.locations = []
        self._cells = []
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

        self._times: list[int] = []
        self._samples: list[np.ndarray] = []  # one (locations, members) array per step

    def __bool__(self):
        return bool(self.locations)

    def observe(self, valid_time: int, members) -> None:
        """Record every member's value at each location for one timestep."""
        if not self.locations:
            return
        members = np.asarray(members)
        self._times.append(valid_time)
        self._samples.append(
            np.stack([members[:, row, column] for row, column in self._cells])
        )

    def series_for(self, index: int) -> list[dict]:
        """The published series for one location, one entry per timestep."""
        entries = []
        for valid_time, sample in zip(self._times, self._samples):
            values = np.asarray(sample[index], dtype=np.float64)
            quantiles = np.percentile(values, PERCENTILES)
            entry = {'t': int(valid_time)}
            for percentile, value in zip(PERCENTILES, quantiles):
                key = 'median' if percentile == 50 else f'p{percentile}'
                entry[key] = _round(value)
            entry['mean'] = _round(values.mean())
            # What the ensemble is really for: how many members say it rains.
            entry['probability'] = round(float((values >= WET_THRESHOLD_MM_H).mean()), 2)
            entries.append(entry)
        return entries


def _round(value: float) -> float:
    # Two decimals is finer than the source's own 0.01 mm/h resolution.
    return round(float(value), 2)


def summarise(series: list[dict], reference_time: int) -> dict:
    """A plain-language read of the series, for a widget with one line of room.

    Describes the *spell* — the contiguous run of wet steps — rather than the
    moment it begins. The onset rate is by construction the instant the median
    crosses the wet threshold, so it is always a small number whether what
    follows is five minutes of drizzle or the leading edge of a downpour; the
    peak, and how far ahead of the onset it sits, is what tells them apart.

    The peak is taken from the current spell only. A second shower three hours
    out is a different event and must not be quoted as this one's severity.

    The structured fields carry the same facts as ``text``, so a caller with
    less room than a sentence can compose its own line instead of parsing this
    one.
    """
    future = [entry for entry in series if entry['t'] >= reference_time]
    if not future:
        return {'raining_now': False, 'text': 'No forecast available.'}

    onset_index = next(
        (i for i, entry in enumerate(future) if entry['median'] >= WET_THRESHOLD_MM_H),
        None,
    )

    if onset_index is None:
        # Even with a dry median, the ensemble may still be hinting at rain.
        best_chance = max(future, key=lambda entry: entry['probability'])
        if best_chance['probability'] >= 0.3:
            return {
                'raining_now': False,
                'starts_at': None,
                'text': (f'Probably dry, but a {round(best_chance["probability"] * 100)}% '
                         f'chance of rain around {_clock(best_chance["t"])}.'),
            }
        return {'raining_now': False, 'starts_at': None, 'text': 'Staying dry.'}

    dry_after = next(
        (i for i in range(onset_index + 1, len(future))
         if future[i]['median'] < WET_THRESHOLD_MM_H),
        None,
    )
    spell = future[onset_index:dry_after]
    clearing = future[dry_after] if dry_after is not None else None

    onset = spell[0]
    peak = max(spell, key=lambda entry: entry['median'])
    raining_now = onset_index == 0

    # Only worth its own clause if it is meaningfully heavier than the start,
    # otherwise it is the same number twice.
    peak_matters = (
        peak['t'] != onset['t']
        and peak['median'] >= onset['median'] * 1.5 + 0.05
    )

    lead_minutes = round((onset['t'] - reference_time) / 60)
    # Whether the sentence counts minutes from now or names clock times. Mixing
    # the two makes "rain at 11:25, up to 4 mm/h within 30 min" ambiguous:
    # thirty minutes from now, or from an onset two hours away?
    relative = raining_now or lead_minutes < 90

    if raining_now:
        parts = [f'Raining now at {_rate(onset["median"])} mm/h']
    else:
        when = f'in {lead_minutes} min' if relative else f'at {_clock(onset["t"])}'
        parts = [f'Dry now — rain {when}' if peak_matters
                 else f'Dry now — rain {when} at {_rate(onset["median"])} mm/h']

    if peak_matters:
        climb = round((peak['t'] - onset['t']) / 60)
        parts.append(
            f'up to {_rate(peak["median"])} mm/h within {climb} min'
            if relative and climb <= 30
            else f'peaking {_rate(peak["median"])} mm/h around {_clock(peak["t"])}'
        )

    parts.append(
        f'easing off around {_clock(clearing["t"])}' if clearing
        else 'lasting past the end of the forecast'
    )

    return {
        'raining_now': raining_now,
        'starts_at': None if raining_now else onset['t'],
        'stops_at': clearing['t'] if clearing else None,
        'peak_mm_h': peak['median'],
        'peak_at': peak['t'],
        'text': ', '.join(parts) + '.',
    }


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
            'value': rain['median'],
            'p10': rain['p10'],
            'p90': rain['p90'],
            'probability': rain['probability'],
        }

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
