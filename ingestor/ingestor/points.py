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
    """A plain-language read of the series, for a widget with one line of room."""
    future = [entry for entry in series if entry['t'] >= reference_time]
    if not future:
        return {'raining_now': False, 'text': 'No forecast available.'}

    raining_now = future[0]['median'] >= WET_THRESHOLD_MM_H
    peak = max(future, key=lambda entry: entry['median'])

    if raining_now:
        clearing = next(
            (entry for entry in future if entry['median'] < WET_THRESHOLD_MM_H), None
        )
        text = (f'Raining now, easing off around {_clock(clearing["t"])}.'
                if clearing else 'Raining for the rest of the forecast.')
        return {
            'raining_now': True,
            'stops_at': clearing['t'] if clearing else None,
            'peak_mm_h': peak['median'],
            'text': text,
        }

    onset = next((entry for entry in future if entry['median'] >= WET_THRESHOLD_MM_H), None)
    if onset is None:
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

    minutes = round((onset['t'] - reference_time) / 60)
    when = f'in {minutes} min' if minutes < 90 else f'at {_clock(onset["t"])}'
    return {
        'raining_now': False,
        'starts_at': onset['t'],
        'peak_mm_h': peak['median'],
        'text': f'Dry now, rain expected {when}.',
    }


def _clock(timestamp: int) -> str:
    import time
    return time.strftime('%H:%M', time.gmtime(timestamp)) + ' UTC'
