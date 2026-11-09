"""Forecast verification: how far ahead the forecast can be trusted.

Every forecast frame predicts one moment, and a few minutes after that moment
the radar measures it. This module pairs the two up and keeps the score. Each
observed frame is scored against every forecast frame on disk that predicted
it, which is one per cycle from the last six hours: one observation, scored at
every lead time. The frames themselves are not archived. Once a pair is scored,
the forecast frame can go, so disk use stays flat while the record grows by
about four megabytes a day.

Two records, in one SQLite file under ``VERIFY_DIR``:

* **The whole map** (``field_scores``). For each forecast cycle and lead time:
  a contingency table at three thresholds, the sums behind bias and mean
  absolute error, and the sums behind a fractions skill score at two scales.
  Every column is a *sum* rather than a ratio, so a month of rows aggregates
  correctly: add them all up, then divide. Averaging per-cycle ratios would
  weight a dry cycle with three wet pixels the same as a day of frontal rain.
  The same row says how often the radar landed below the band's p10 and above
  its p90. That is the calibration check: if the band is honest, each should
  happen about 10% of the time. And it scores persistence, the radar at the
  cycle's reference time held still, the same way. That baseline is what lets
  one month be compared with another: showers are harder than rain bands at
  every lead, so raw scores mostly measure the weather, and the gap over
  persistence measures the forecast.

* **The configured locations** (``point_forecast`` and ``point_observed``).
  The numbers each point document published, kept per cycle and lead time, and
  what the radar measured at that pixel and within the two radii the forecast
  numbers describe. They are joined on the valid time when queried, so a
  forecast and its observation can arrive in either order.

The comparison is made on the published frames, not on KNMI's raw files. That
is deliberate: the published frames are what a reader actually sees, so they
are the numbers whose accuracy is worth knowing, and both sides of every
comparison already share one output grid pixel for pixel. Pixels the radar did
not measure are left out on both sides rather than counted as dry.

Read it back with ``python -m ingestor.verify``, which prints skill by lead
time, or with ``--by month`` to put months side by side. See :func:`report`.
"""

from __future__ import annotations

import argparse
import io
import json
import logging
import math
import os
import sqlite3
import sys
import time
from contextlib import closing

import numpy as np
from PIL import Image

from .blend import WET_MM_H, neighbourhood_maximum, pooled_cell_reach
from .encode import _to_log_byte, decode_frame
from .raster import mercator_y

log = logging.getLogger('ingestor.verify')

#: Has a ``.sqlite`` extension so the host's nightly backup finds it and takes a
#: consistent copy rather than copying a file that might be mid-write.
DB_NAME = 'verification.sqlite'

#: Thresholds for the contingency tables, in mm/h. These are the bottom of the
#: colour ramp (anything the map paints), rain you would notice, and a downpour.
#: The last part of each column name (``t01``, ``t1``, ``t5``) is the threshold.
THRESHOLDS = ((0.1, 't01'), (1.0, 't1'), (5.0, 't5'))

#: Fractions skill score: threshold in mm/h, and window sides in output pixels
#: (about 1 km each). 5 km asks "roughly here"; 25 km asks "in this region".
FSS_THRESHOLD_MM_H = 1.0
FSS_WINDOWS = (5, 25)

#: The longest lead KNMI publishes, plus an hour of slack. A cycle older than
#: this has nothing left that a new observation could score.
MAX_LEAD_SECONDS = 7 * 3600

def is_forecast(reference: int, valid: int) -> bool:
    """Whether a frame predicts anything at all.

    The stand-in publishes a step at its own reference time (lead 0). That is
    the radar picture it started from, not a forecast: scored, it comes out
    near perfect and props up the shortest lead. The primary's first step is
    normally +5 min, but the same rule holds for it.
    """
    return valid > reference


#: How long radar frames stay on disk while verifying: the longest lead plus
#: half an hour. The radar at a cycle's reference time is that cycle's
#: persistence forecast, so it has to outlive the cycle's last lead.
OBSERVED_KEEP_SECONDS = 6 * 3600 + 1800

SCHEMA = """
CREATE TABLE IF NOT EXISTS cycles (
    reference_time    INTEGER PRIMARY KEY,
    recorded_at       INTEGER NOT NULL,
    dataset           TEXT NOT NULL,
    fallback          INTEGER NOT NULL,
    members           INTEGER,
    reducer           TEXT,
    max_precip        REAL NOT NULL,
    neighbourhood_km  REAL,
    spread            TEXT,      -- the manifest's spread block, as JSON; NULL when off
    estimated         TEXT       -- JSON list of valid times KNMI left dead
);

CREATE TABLE IF NOT EXISTS field_scores (
    reference_time  INTEGER NOT NULL,
    lead_min        INTEGER NOT NULL,
    scored_at       INTEGER NOT NULL,
    measured        INTEGER NOT NULL,  -- pixels the radar saw; the denominator
    hits_t01 INTEGER, misses_t01 INTEGER, false_alarms_t01 INTEGER,
    hits_t1  INTEGER, misses_t1  INTEGER, false_alarms_t1  INTEGER,
    hits_t5  INTEGER, misses_t5  INTEGER, false_alarms_t5  INTEGER,
    sum_forecast  REAL,   -- mm/h summed over measured pixels
    sum_observed  REAL,
    sum_abs_error REAL,
    fss_num_5  REAL, fss_den_5  REAL,
    fss_num_25 REAL, fss_den_25 REAL,
    band_pixels  INTEGER,  -- pixels where the band or the radar had rain nearby
    below_p10    INTEGER,
    below_p50    INTEGER,
    above_p50    INTEGER,
    above_p90    INTEGER,
    -- Persistence: the radar at the reference time, held still and scored the
    -- same way. The baseline that tells a hard month from a bad forecast.
    -- NULL when that radar frame was missing.
    p_hits_t01 INTEGER, p_misses_t01 INTEGER, p_false_alarms_t01 INTEGER,
    p_hits_t1  INTEGER, p_misses_t1  INTEGER, p_false_alarms_t1  INTEGER,
    p_hits_t5  INTEGER, p_misses_t5  INTEGER, p_false_alarms_t5  INTEGER,
    p_sum_forecast  REAL,
    p_sum_abs_error REAL,
    p_fss_num_5  REAL, p_fss_den_5  REAL,
    p_fss_num_25 REAL, p_fss_den_25 REAL,
    PRIMARY KEY (reference_time, lead_min)
) WITHOUT ROWID;

-- Rates are stored as integer hundredths of a mm/h and probabilities as whole
-- percent. That is the published precision anyway, and SQLite stores small
-- integers in one or two bytes where a REAL always takes eight.
CREATE TABLE IF NOT EXISTS point_forecast (
    location        TEXT NOT NULL,
    reference_time  INTEGER NOT NULL,
    lead_min        INTEGER NOT NULL,
    field           INTEGER,
    median          INTEGER,
    p10             INTEGER,
    p90             INTEGER,
    probability     INTEGER,
    probability_nearby INTEGER,
    nearby_p10      INTEGER,
    nearby_median   INTEGER,
    nearby_p90      INTEGER,
    estimated       INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (location, reference_time, lead_min)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS point_observed (
    location    TEXT NOT NULL,
    valid_time  INTEGER NOT NULL,
    measured    INTEGER,   -- at the location's pixel; NULL if no radar saw it
    max_nearby  INTEGER,   -- wettest pixel within the neighbourhood radius
    max_spread  INTEGER,   -- wettest within the spread radius, like nearby_p*
    PRIMARY KEY (location, valid_time)
) WITHOUT ROWID;
"""


def enabled(config) -> bool:
    return bool(getattr(config, 'verify_dir', ''))


def connect(directory: str) -> sqlite3.Connection:
    os.makedirs(directory, exist_ok=True)
    db = sqlite3.connect(os.path.join(directory, DB_NAME), timeout=30)
    # WAL so the report can read while the ingestor writes, and so the backup's
    # reader never blocks a commit.
    db.execute('PRAGMA journal_mode=WAL')
    db.execute('PRAGMA synchronous=NORMAL')
    db.executescript(SCHEMA)
    return db


def _centi(value):
    return None if value is None else int(round(value * 100))


def _percent(value):
    return None if value is None else int(round(value * 100))


# ------------------------------------------------------------------ recording


def record_cycle(config, source, meta: dict, frames: list, extractor) -> None:
    """Remember one published cycle, and what it said at each location.

    Called once per cycle, after its frames are on disk. Without this row the
    cycle's frames are never scored and never held back from pruning, so a
    cycle from before verification was switched on is simply left alone.
    """
    if not enabled(config):
        return
    reference_time = int(meta['reference_time'])
    estimated = sorted(int(frame['t']) for frame in frames if frame.get('estimated'))
    rows = []
    if extractor:
        for index, location in enumerate(extractor.locations):
            for entry in extractor.series_for(index):
                if not is_forecast(reference_time, entry['t']):
                    continue
                lead = (entry['t'] - reference_time) // 60
                rows.append((
                    location.name, reference_time, lead,
                    _centi(entry.get('field')), _centi(entry.get('median')),
                    _centi(entry.get('p10')), _centi(entry.get('p90')),
                    _percent(entry.get('probability')),
                    _percent(entry.get('probability_nearby')),
                    _centi(entry.get('nearby_p10')), _centi(entry.get('nearby_median')),
                    _centi(entry.get('nearby_p90')),
                    1 if entry.get('estimated') else 0,
                ))
    fallback = 1 if getattr(source, 'is_fallback', False) else 0
    try:
        with closing(connect(config.verify_dir)) as db, db:
            previous = db.execute('SELECT fallback FROM cycles WHERE reference_time = ?',
                                  (reference_time,)).fetchone()
            if previous is not None and previous[0] != fallback:
                # The other product already published this reference time. Both
                # run on the same 5-minute clock, so the primary's first cycle
                # after an outage can land on the stand-in's last one, and its
                # frames have just overwritten the stand-in's files of the same
                # name. Whatever was scored from those files belongs to the
                # other product, and keeping it under this row's flag would
                # count it as this one. It is dropped instead. The radar frames
                # are still on disk, so the next pass scores the same leads
                # again from the frames now there.
                dropped = db.execute('DELETE FROM field_scores WHERE reference_time = ?',
                                     (reference_time,)).rowcount
                db.execute('DELETE FROM point_forecast WHERE reference_time = ?',
                           (reference_time,))
                log.info('verification: cycle %d is now %s; dropped %d scores from the %s',
                         reference_time, 'the stand-in' if fallback else 'the primary',
                         dropped, 'primary' if fallback else 'stand-in')
            db.execute(
                'INSERT OR REPLACE INTO cycles VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
                (reference_time, int(time.time()),
                 getattr(source, 'dataset_label', None) or config.dataset,
                 fallback,
                 getattr(source, 'member_count', None), meta.get('reducer'),
                 float(config.max_precip),
                 getattr(extractor, 'neighbourhood_km', None) if extractor else None,
                 json.dumps(meta['spread']) if meta.get('spread') else None,
                 json.dumps(estimated)))
            db.executemany(
                'INSERT OR REPLACE INTO point_forecast VALUES '
                '(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)', rows)
    except sqlite3.Error as error:
        # Verification is a side record. A full disk or a locked file must not
        # cost the map a forecast cycle.
        log.warning('verification: could not record cycle %d (%s)', reference_time, error)


# ------------------------------------------------------------------ pruning


def pending(config, forecast_frames, cutoff: int) -> set:
    """The ``(reference, valid)`` forecast frames still waiting to be scored.

    These are held back from pruning. ``cutoff`` is the oldest observation that
    can still turn up on disk. A frame predicting a time before that will never
    be scored (KNMI published no radar for it, say), so it is let go rather than
    kept forever.
    """
    if not enabled(config) or not forecast_frames:
        return set()
    oldest = min(reference for reference, _ in forecast_frames)
    try:
        with closing(connect(config.verify_dir)) as db:
            tracked = {row[0] for row in db.execute(
                'SELECT reference_time FROM cycles WHERE reference_time >= ?', (oldest,))}
            scored = {(reference, reference + lead * 60) for reference, lead in db.execute(
                'SELECT reference_time, lead_min FROM field_scores WHERE reference_time >= ?',
                (oldest,))}
    except sqlite3.Error as error:
        log.warning('verification: could not read pending frames (%s)', error)
        return set()
    return {(reference, valid) for reference, valid in forecast_frames
            if reference in tracked and valid >= cutoff and is_forecast(reference, valid)
            and (reference, valid) not in scored}


# ------------------------------------------------------------------ scoring


def verify_observed(config, grid, forecast_frames, observed_times, names) -> int:
    """Score every observed frame on disk against what predicted it.

    ``names`` maps the frame kinds to file names: ``names.forecast(r, v)``,
    ``names.spread(r, v)`` and ``names.observed(v)``. They are passed in so this
    module does not have to import :mod:`ingestor.main`, which imports it.

    Idempotent: whatever is already scored is skipped, so it is safe to run on
    every poll. Returns how many forecast frames it scored.
    """
    if not enabled(config) or not observed_times:
        return 0
    started = time.monotonic()
    newest_reference = max((r for r, _ in forecast_frames), default=0)
    since = min(observed_times) - MAX_LEAD_SECONDS
    scored_pairs = 0
    try:
        with closing(connect(config.verify_dir)) as db:
            cycles = {row[0]: row for row in db.execute(
                'SELECT reference_time, max_precip, spread, neighbourhood_km FROM cycles '
                'WHERE reference_time >= ?', (since,))}
            scored = {(r, r + lead * 60) for r, lead in db.execute(
                'SELECT reference_time, lead_min FROM field_scores WHERE reference_time >= ?',
                (since,))}
            observed_points = {(location, valid) for location, valid in db.execute(
                'SELECT location, valid_time FROM point_observed WHERE valid_time >= ?',
                (min(observed_times),))}

            waiting = {}
            for reference, valid in forecast_frames:
                if (valid in observed_times and reference in cycles
                        and is_forecast(reference, valid)
                        and (reference, valid) not in scored):
                    waiting.setdefault(valid, []).append(reference)

            points = _point_geometry(config, grid, cycles.get(newest_reference))

            for valid in sorted(observed_times):
                todo = sorted(waiting.get(valid, []))
                need_points = [p for p in points if (p['name'], valid) not in observed_points]
                if not todo and not need_points:
                    continue
                path = os.path.join(config.frame_dir, names.observed(valid))
                try:
                    with open(path, 'rb') as handle:
                        observed, measured = decode_frame(handle.read(), config.max_precip)
                except (OSError, ValueError) as error:
                    log.warning('verification: could not read %s (%s)', path, error)
                    continue

                # Everything about the radar side of a comparison is the same for
                # each of the ~48 forecasts that predicted this moment, so it is
                # worked out once here rather than once per pair.
                observation = Observation(observed, measured, grid)
                with db:
                    for point in need_points:
                        db.execute('INSERT OR REPLACE INTO point_observed VALUES (?, ?, ?, ?, ?)',
                                   (point['name'], valid,
                                    *_observe_point(point, observed, measured)))
                    for reference in todo:
                        row = _score_pair(config, names, cycles[reference],
                                          reference, valid, observation)
                        if row is not None:
                            db.execute(
                                'INSERT OR REPLACE INTO field_scores VALUES '
                                f'({", ".join("?" * len(row))})', row)
                            scored_pairs += 1
    except sqlite3.Error as error:
        log.warning('verification: scoring stopped (%s)', error)
    if scored_pairs:
        log.info('verification: scored %d forecast frames in %.1f s',
                 scored_pairs, time.monotonic() - started)
    return scored_pairs


def _score_pair(config, names, cycle, reference, valid, observation):
    """One ``field_scores`` row, or None if the forecast frame is unreadable."""
    _, max_precip, spread_json, _ = cycle
    path = os.path.join(config.frame_dir, names.forecast(reference, valid))
    try:
        with open(path, 'rb') as handle:
            forecast, _ = decode_frame(handle.read(), max_precip)
    except (OSError, ValueError) as error:
        log.warning('verification: could not read %s (%s)', path, error)
        return None
    if forecast.shape != observation.shape:
        # The grid changed between the cycle and the radar frame. The observed
        # frames are thrown away when that happens, so this is a race and the
        # pair is simply not comparable.
        return None

    scores = observation.field_scores(forecast)
    band = (None,) * 5
    if spread_json:
        band = _band_scores(config, names, json.loads(spread_json), reference, valid,
                            observation, max_precip)
    persistence = _persistence_scores(config, names, reference, observation)
    return (reference, (valid - reference) // 60, int(time.time()), *scores, *band,
            *persistence)


#: Which of :meth:`Observation.field_scores`'s columns persistence keeps: the
#: contingency tables, the forecast sum, the absolute error and the FSS sums.
#: The pixel count and the observed sum are the forecast's own row already.
_PERSISTENCE_COLUMNS = (*range(1, 11), 12, *range(13, 17))


def _persistence_scores(config, names, reference, observation):
    """The ``p_*`` columns: the radar at ``reference``, scored as a forecast.

    Pixels that radar frame did not see are forecast dry. The radar's coverage
    barely moves from one frame to the next, so in practice those are the
    pixels the frame being scored did not see either, and are left out anyway.
    """
    path = os.path.join(config.frame_dir, names.observed(reference))
    try:
        with open(path, 'rb') as handle:
            then, seen = decode_frame(handle.read(), config.max_precip)
    except (OSError, ValueError):
        return (None,) * len(_PERSISTENCE_COLUMNS)
    if then.shape != observation.shape:
        return (None,) * len(_PERSISTENCE_COLUMNS)
    row = observation.field_scores(np.where(seen, then, 0.0))
    return tuple(row[index] for index in _PERSISTENCE_COLUMNS)


class Observation:
    """One radar frame, made ready to be compared with many forecasts.

    Unmeasured pixels are zeroed. They then count as agreeing "dry" with any
    forecast that is zeroed the same way, which adds nothing to hits, misses or
    false alarms, and the denominator (``measured``) leaves them out.
    """

    def __init__(self, observed, measured, grid=None):
        self.measured = np.asarray(measured, dtype=bool)
        self.observed = np.where(self.measured, observed, 0.0).astype(np.float32)
        self.shape = self.observed.shape
        self.grid = grid
        self.count = int(np.count_nonzero(self.measured))
        self.wet = [self.observed >= threshold for threshold, _ in THRESHOLDS]
        self.total = float(self.observed.sum(dtype=np.float64))
        wet = self.observed >= FSS_THRESHOLD_MM_H
        seen = self.measured.astype(np.int32)
        # Per window: the pixels whose whole window the radar saw, and the
        # radar's wet fraction there. A window half off the radar's edge would
        # compare a fraction of a fraction, so those are left out.
        self.fractions = []
        for window in FSS_WINDOWS:
            whole = _box_sum(seen, window) == window * window
            self.fractions.append(
                (whole, _box_sum(wet, window)[whole] / float(window * window)))
        self._bands = {}

    def field_scores(self, forecast) -> tuple:
        """The whole-map columns of a ``field_scores`` row, in table order."""
        forecast = np.where(self.measured, forecast, 0.0).astype(np.float32)
        row = [self.count]
        for (threshold, _), wet_observed in zip(THRESHOLDS, self.wet):
            wet_forecast = forecast >= threshold
            hits = int(np.count_nonzero(wet_forecast & wet_observed))
            row += [hits, int(np.count_nonzero(wet_observed)) - hits,
                    int(np.count_nonzero(wet_forecast)) - hits]
        row += [float(forecast.sum(dtype=np.float64)), self.total,
                float(np.abs(forecast - self.observed).sum(dtype=np.float64))]
        wet_forecast = forecast >= FSS_THRESHOLD_MM_H
        for window, (whole, observed) in zip(FSS_WINDOWS, self.fractions):
            predicted = _box_sum(wet_forecast, window)[whole] / float(window * window)
            row += [float(((predicted - observed) ** 2).sum(dtype=np.float64)),
                    float((predicted ** 2 + observed ** 2).sum(dtype=np.float64))]
        return tuple(row)

    def near(self, spread, factor: int, rows: int, columns: int, max_precip: float):
        """The radar as the band sees it: ``(log bytes, fully seen)``.

        Cached per band geometry, since every forecast of one cycle family
        shares it.
        """
        key = (float(spread['radius_km']), factor, rows, columns, max_precip)
        if key not in self._bands:
            pooled = _pool(self.observed[:rows * factor, :columns * factor], factor,
                           np.maximum)
            seen = _pool(self.measured[:rows * factor, :columns * factor], factor,
                         np.logical_and)
            reach_rows, reach_columns = _spread_reach(self.grid, spread, factor)
            near = neighbourhood_maximum(pooled, reach_rows, 0)
            near = neighbourhood_maximum(near, reach_columns, 1)
            # A pixel counts only if every pixel within its reach was measured:
            # an unmeasured neighbour could have held the rain the band is about.
            blind = neighbourhood_maximum((~seen).astype(np.float32), reach_rows, 0)
            blind = neighbourhood_maximum(blind, reach_columns, 1)
            self._bands[key] = (_to_log_byte(near, max_precip), blind == 0)
        return self._bands[key]


def field_scores(forecast, observed, measured) -> tuple:
    """The whole-map columns for one pair, without the per-frame caching."""
    return Observation(observed, measured).field_scores(forecast)


def _box_sum(values, window: int):
    """Sum over a ``window`` x ``window`` square centred on each pixel.

    Uses an integral image, so the cost does not depend on the window size.
    Outside the array counts as zero. Integer when given booleans or integers,
    which keeps the counts exact.
    """
    half = window // 2
    kind = np.int32 if values.dtype.kind in 'biu' else np.float64
    integral = np.zeros((values.shape[0] + window, values.shape[1] + window), dtype=kind)
    integral[half + 1:half + 1 + values.shape[0], half + 1:half + 1 + values.shape[1]] = values
    integral.cumsum(axis=0, out=integral)
    integral.cumsum(axis=1, out=integral)
    rows, columns = values.shape
    return (integral[window:window + rows, window:window + columns]
            - integral[:rows, window:window + columns]
            - integral[window:window + rows, :columns]
            + integral[:rows, :columns])


def _band_scores(config, names, spread, reference, valid, observation, max_precip):
    """Where the radar landed against the band: the five ``band`` columns.

    The band is not a statement about one pixel. It is the percentiles of what
    each member put *within the spread radius* of it, on a grid pooled by
    ``downsample``. So the radar is put through the same steps before it is
    compared: pooled by maximum, then dilated by the same reach. The
    comparison is made in the band's own log-byte encoding, so the 2.8% steps
    of that encoding cannot be read as the radar falling just outside the band.

    There is one difference. The forecast was dilated on KNMI's grid and then
    resampled, while the radar is dilated on the published grid. Both are about
    1 km and the radius is a few, so it is a small fraction of a cell.
    """
    nothing = (None,) * 5
    path = os.path.join(config.frame_dir, names.spread(reference, valid))
    try:
        with open(path, 'rb') as handle:
            rgba = np.array(Image.open(io.BytesIO(handle.read())).convert('RGBA'))
    except (OSError, ValueError):
        return nothing
    factor = int(spread.get('downsample') or 1)
    rows, columns = rgba.shape[:2]
    if (observation.shape[0] // factor != rows or observation.shape[1] // factor != columns
            or observation.grid is None):
        return nothing

    radar, seen = observation.near(spread, factor, rows, columns, max_precip)
    p10, p50, p90 = rgba[:, :, 0], rgba[:, :, 1], rgba[:, :, 2]
    # Pixels where neither side has any rain nearby are left out. There the
    # whole band and the radar are all zero, which says nothing about whether
    # the band is calibrated and would bury the pixels that do.
    relevant = seen & ((radar > 0) | (p90 > 0))
    count = lambda mask: int(np.count_nonzero(relevant & mask))
    return (int(np.count_nonzero(relevant)), count(radar < p10), count(radar < p50),
            count(radar > p50), count(radar > p90))


def _pool(values, factor: int, reducer):
    if factor <= 1:
        return values
    out = values[0::factor, 0::factor].copy()
    for row in range(factor):
        for column in range(factor):
            if row or column:
                out = reducer(out, values[row::factor, column::factor])
    return out


def _spread_reach(grid, spread, factor):
    """The pooled reach the band was built with, worked out on the published grid."""
    dlon = (grid.east - grid.west) / grid.width
    middle = (grid.north + grid.south) / 2
    # Mercator rows are not even in latitude, so take the spacing at the middle,
    # as :func:`ingestor.main.spread_plan` does for the source grid.
    dlat = math.degrees((mercator_y(grid.north) - mercator_y(grid.south))
                        / grid.height) * math.cos(math.radians(middle))
    return pooled_cell_reach(float(spread['radius_km']), dlat, dlon, middle, factor)


# ------------------------------------------------------------------ locations


def _point_geometry(config, grid, cycle):
    """The pixels each configured location is observed at, on the published grid.

    Three readings per location, one for each kind of forecast number:
    the location's own pixel (``field``, ``median``, ``probability``), the
    wettest pixel within the neighbourhood radius (``probability_nearby``),
    and the wettest within the spread radius (``nearby_p10`` .. ``nearby_p90``).
    """
    if not config.widget_locations or grid is None:
        return []
    neighbourhood = float((cycle and cycle[3]) or config.neighbourhood_km)
    spread_radius = config.spread_radius_km
    dlon = (grid.east - grid.west) / grid.width
    points = []
    for location in config.widget_locations:
        pixel = grid.pixel_for(location.lon, location.lat)
        if pixel is None:
            continue
        dlat = math.degrees((mercator_y(grid.north) - mercator_y(grid.south))
                            / grid.height) * math.cos(math.radians(location.lat))
        points.append({
            'name': location.name, 'pixel': pixel,
            'disc': _disc(pixel, grid, dlat, dlon, location.lat, neighbourhood, round_=True),
            'square': (None if spread_radius is None else
                       _disc(pixel, grid, dlat, dlon, location.lat, spread_radius, round_=False)),
        })
    return points


def _disc(pixel, grid, dlat, dlon, latitude, radius_km, round_: bool):
    """Index arrays for a disc (``round_``) or square of ``radius_km``."""
    km_row = dlat * 111.32
    km_column = dlon * 111.32 * math.cos(math.radians(latitude))
    reach_rows = max(1, int(math.ceil(radius_km / km_row)))
    reach_columns = max(1, int(math.ceil(radius_km / km_column)))
    dy, dx = np.meshgrid(np.arange(-reach_rows, reach_rows + 1),
                         np.arange(-reach_columns, reach_columns + 1), indexing='ij')
    inside = np.ones(dy.shape, dtype=bool)
    if round_:
        inside = np.hypot(dy * km_row, dx * km_column) <= radius_km
    rows, columns = pixel[0] + dy, pixel[1] + dx
    inside &= (rows >= 0) & (rows < grid.height) & (columns >= 0) & (columns < grid.width)
    return rows[inside], columns[inside]


def _observe_point(point, observed, measured):
    """``(measured, max_nearby, max_spread)`` in hundredths of a mm/h.

    NULL rather than zero wherever the radar missed any of the pixels in
    question: "the wettest pixel nearby" is not knowable with some unseen.
    """
    row, column = point['pixel']
    at = _centi(float(observed[row, column])) if measured[row, column] else None

    def wettest(area):
        if area is None:
            return None
        rows, columns = area
        if not measured[rows, columns].all():
            return None
        return _centi(float(observed[rows, columns].max(initial=0.0)))

    return at, wettest(point['disc']), wettest(point['square'])


# ------------------------------------------------------------------ report

#: Lead-time buckets for the report, in minutes. A lead falls in the first
#: bucket at or above it.
BUCKETS = (5, 10, 15, 30, 45, 60, 90, 120, 180, 240, 300, 360)

#: The two leads the monthly summary compares, as buckets.
SUMMARY_LEADS = (60, 180)

#: FSS at or above this counts as "useful" for the summary's horizon column.
#: 0.5 is the usual rule of thumb: halfway between no skill and perfect.
USEFUL_FSS = 0.5


def _bucket(lead):
    return next((b for b in BUCKETS if lead <= b), BUCKETS[-1])


def _label(bucket):
    return f'≤{bucket}m' if bucket < 60 or bucket % 60 else f'≤{bucket // 60}h'


#: Sums the report needs from ``field_scores``, by name. The ``paired_*`` ones
#: are the forecast's own sums over just the rows that have a persistence
#: score, so the two are compared over exactly the same moments.
_FIELD_SUMS = {
    'pairs': 'COUNT(*)',
    'measured': 'SUM(measured)',
    'hits': 'SUM(hits_t1)', 'misses': 'SUM(misses_t1)', 'false_alarms': 'SUM(false_alarms_t1)',
    'hits01': 'SUM(hits_t01)', 'misses01': 'SUM(misses_t01)',
    'false_alarms01': 'SUM(false_alarms_t01)',
    'abs_error': 'SUM(sum_abs_error)',
    'num5': 'SUM(fss_num_5)', 'den5': 'SUM(fss_den_5)',
    'num25': 'SUM(fss_num_25)', 'den25': 'SUM(fss_den_25)',
    'band': 'SUM(band_pixels)', 'below': 'SUM(below_p10)', 'above': 'SUM(above_p90)',
    'p_hits': 'SUM(p_hits_t1)', 'p_misses': 'SUM(p_misses_t1)',
    'p_false_alarms': 'SUM(p_false_alarms_t1)',
    'p_num25': 'SUM(p_fss_num_25)', 'p_den25': 'SUM(p_fss_den_25)',
    'paired_num25': 'SUM(CASE WHEN p_fss_num_25 IS NOT NULL THEN fss_num_25 END)',
    'paired_den25': 'SUM(CASE WHEN p_fss_num_25 IS NOT NULL THEN fss_den_25 END)',
}


def _field_buckets(db, start, end, fallback, group='NULL'):
    """``{group: {bucket: sums}}`` over ``[start, end)``."""
    names = list(_FIELD_SUMS)
    rows = db.execute(f"""
        SELECT {group}, f.lead_min, {', '.join(_FIELD_SUMS[name] for name in names)}
        FROM field_scores f JOIN cycles c USING (reference_time)
        WHERE f.reference_time >= ? AND f.reference_time < ? AND f.lead_min > 0 {fallback}
        GROUP BY 1, 2""", (start, end)).fetchall()
    out = {}
    for key, lead, *values in rows:
        totals = out.setdefault(key, {}).setdefault(_bucket(lead), dict.fromkeys(names, 0))
        for name, value in zip(names, values):
            totals[name] += value or 0
    return out


def _csi(hits, misses, false_alarms):
    total = hits + misses + false_alarms
    return hits / total if total else None


def _fss(num, den):
    return 1 - num / den if den else None


#: Which cycles a report covers, as the SQL that picks them out.
PRODUCTS = {
    'primary': ('AND c.fallback = 0', ''),
    'all': ('', ', stand-in included'),
    'fallback': ('AND c.fallback = 1', ', stand-in only'),
}


def report(directory: str, days: float | None = 30.0, products: str = 'primary',
           month: str | None = None, by_month: bool = False, out=sys.stdout) -> None:
    """Print skill by lead time, for the map and for each location.

    ``month`` (``YYYY-MM``) narrows it to one calendar month. ``by_month``
    prints one summary line per month instead, over ``days`` if given and over
    everything otherwise. Months are in the container's local time zone.

    ``products`` picks the cycles: ``primary`` (the default), ``fallback`` for
    the deterministic stand-in alone, or ``all``. The stand-in is one run with
    no spread layer, so its band columns read "–" and its location
    probabilities are only ever 0 or 100%.

    How to read it:

    * ``CSI`` (critical success index) is hits / (hits + misses + false alarms)
      at 1 mm/h: 1 is perfect, 0 is no skill. ``POD`` is the share of observed
      rain that was forecast, ``FAR`` the share of forecast rain that did not
      happen, and ``bias`` how much more often it forecast rain than it rained.
    * ``FSS`` (fractions skill score) at 1 mm/h asks whether the right amount of
      rain fell in each 5 km or 25 km square, so a shower a few km out of place
      is not scored as both a miss and a false alarm. Above about 0.5 is
      usually called "useful" at that scale.
    * ``pCSI`` and ``pFSS25`` are the same scores for persistence: the radar at
      the moment the forecast was made, held still. The forecast earns its keep
      where it beats them. Comparing months on this gap rather than on the raw
      scores separates a bad month for the forecast from a month of weather
      that is hard to forecast, such as summer showers.
    * ``<p10`` and ``>p90`` are how often the radar fell below or above the
      band, over pixels where either side had rain nearby. 10% each means the
      band is honest; more means it is too narrow.
    * For locations, ``BSS`` is the Brier skill score of the probability against
      always forecasting the sample's own rain frequency, and ``vs pers``
      against persistence ("it will do what it is doing now"). Above 0 beats it.
    """
    path = os.path.join(directory, DB_NAME)
    if not os.path.exists(path):
        print(f'no verification data at {path}', file=out)
        return
    fallback, scope_note = PRODUCTS[products]
    if month:
        start, end = _month_range(month)
        scope = f'{month}'
    elif days is None:
        start, end, scope = 0, 2 ** 62, 'all data'
    else:
        start, end, scope = int(time.time() - days * 86400), 2 ** 62, f'last {days:g} days'
    scope += scope_note

    db = sqlite3.connect(f'file:{path}?mode=ro', uri=True)
    with closing(db):
        if by_month:
            _report_months(db, start, end, fallback, scope, out)
            return
        buckets = _field_buckets(db, start, end, fallback).get(None)
        if not buckets:
            print(f'no scored forecasts ({scope})', file=out)
            return
        cycles = db.execute(
            f'SELECT COUNT(DISTINCT reference_time) FROM field_scores f JOIN cycles c '
            f'USING (reference_time) WHERE f.reference_time >= ? AND f.reference_time < ? '
            f'AND f.lead_min > 0 {fallback}', (start, end)).fetchone()[0]
        print(f'Whole map, {scope} ({cycles} cycles).', file=out)
        print('Scores at 1 mm/h; CSI@0.1 at the bottom of the colour ramp. '
              'p = persistence.', file=out)
        print(f'{"lead":>6} {"pairs":>6} {"CSI":>5} {"pCSI":>5} {"POD":>5} {"FAR":>5} '
              f'{"bias":>5} {"CSI@.1":>6} {"MAE":>6} {"FSS5":>5} {"FSS25":>5} {"pFSS25":>6} '
              f'{"<p10":>5} {">p90":>5}', file=out)
        for bucket in sorted(buckets):
            t = buckets[bucket]
            print(f'{_label(bucket):>6} {t["pairs"]:>6} '
                  f'{_num(_csi(t["hits"], t["misses"], t["false_alarms"]))} '
                  f'{_num(_csi(t["p_hits"], t["p_misses"], t["p_false_alarms"]))} '
                  f'{_ratio(t["hits"], t["hits"] + t["misses"])} '
                  f'{_ratio(t["false_alarms"], t["hits"] + t["false_alarms"])} '
                  f'{_ratio(t["hits"] + t["false_alarms"], t["hits"] + t["misses"])} '
                  f'{_num(_csi(t["hits01"], t["misses01"], t["false_alarms01"]), 6)} '
                  f'{_ratio(t["abs_error"], t["measured"], width=6, digits=3)} '
                  f'{_num(_fss(t["num5"], t["den5"]))} {_num(_fss(t["num25"], t["den25"]))} '
                  f'{_num(_fss(t["p_num25"], t["p_den25"]), 6)} '
                  f'{_percent_of(t["below"], t["band"])} {_percent_of(t["above"], t["band"])}',
                  file=out)

        _report_points(db, start, end, fallback, out)


def _report_months(db, start, end, fallback, scope, out):
    """One line per month: how wet it was, and how the forecast held up."""
    months = _field_buckets(
        db, start, end, fallback,
        group="strftime('%Y-%m', f.reference_time, 'unixepoch', 'localtime')")
    if not months:
        print(f'no scored forecasts ({scope})', file=out)
        return
    print(f'By month, {scope}. Scores at 1 mm/h, FSS at 25 km.', file=out)
    print('"vs p" is FSS skill against persistence, (FSS - pFSS) / (1 - pFSS): 0 is no',
          file=out)
    print('better than holding the radar still, 1 is perfect. Compare months on that column:',
          file=out)
    print('raw scores mostly say how hard the weather was. "useful to" is the last lead',
          file=out)
    print(f'before FSS first drops below {USEFUL_FSS}.', file=out)
    header = f'{"month":>7} {"pairs":>6} {"rain%":>6}'
    for lead in SUMMARY_LEADS:
        tag = _label(lead).lstrip('≤')
        header += f' {"CSI " + tag:>7} {"FSS " + tag:>7} {"vs p " + tag:>8}'
    header += f' {"useful to":>9} {"<p10":>5} {">p90":>5}'
    print(header, file=out)
    for month in sorted(months):
        buckets = months[month]
        totals = {name: sum(b[name] for b in buckets.values()) for name in _FIELD_SUMS}
        line = (f'{month:>7} {totals["pairs"]:>6} '
                f'{_percent_of(totals["hits"] + totals["misses"], totals["measured"], 6, 1)}')
        for lead in SUMMARY_LEADS:
            t = buckets.get(lead)
            if t is None:
                line += f' {"–":>7} {"–":>7} {"–":>8}'
                continue
            gain = _skill_vs(_fss(t['paired_num25'], t['paired_den25']),
                             _fss(t['p_num25'], t['p_den25']))
            line += (f' {_num(_csi(t["hits"], t["misses"], t["false_alarms"]), 7)}'
                     f' {_num(_fss(t["num25"], t["den25"]), 7)}'
                     f' {_num(gain, 8)}')
        useful = _horizon(buckets, lambda t: (_fss(t['num25'], t['den25']) or 0) >= USEFUL_FSS)
        line += (f' {useful:>9} '
                 f'{_percent_of(totals["below"], totals["band"])} '
                 f'{_percent_of(totals["above"], totals["band"])}')
        print(line, file=out)
    print('\nFor one month in full, by lead time and per location: --month YYYY-MM', file=out)


def _skill_vs(score, reference):
    """Skill of ``score`` against ``reference`` on a scale where 1 is perfect."""
    if score is None or reference is None or reference >= 1:
        return None
    return (score - reference) / (1 - reference)


def _horizon(buckets, holds):
    """The longest lead bucket up to which ``holds`` is true without a break."""
    reached = None
    for bucket in sorted(buckets):
        if not holds(buckets[bucket]):
            break
        reached = bucket
    return _label(reached).lstrip('≤') if reached else '–'


def _month_range(month: str):
    """``[start, end)`` of a ``YYYY-MM`` month in local time, as epoch seconds."""
    year, number = (int(part) for part in month.split('-'))
    if not 1 <= number <= 12:
        raise ValueError(f'no month {number}')
    following = (year + number // 12, number % 12 + 1)
    first = time.mktime((year, number, 1, 0, 0, 0, 0, 0, -1))
    after = time.mktime((*following, 1, 0, 0, 0, 0, 0, -1))
    return int(first), int(after)


def _report_points(db, start, end, fallback, out):
    rows = db.execute(f"""
        SELECT f.location, f.lead_min,
               f.probability, f.probability_nearby, f.field,
               o.measured, o.max_nearby,
               earlier.measured, earlier.max_nearby
        FROM point_forecast f
        JOIN cycles c USING (reference_time)
        JOIN point_observed o
          ON o.location = f.location AND o.valid_time = f.reference_time + f.lead_min * 60
        LEFT JOIN point_observed earlier
          ON earlier.location = f.location AND earlier.valid_time = f.reference_time
        WHERE f.reference_time >= ? AND f.reference_time < ? AND f.lead_min > 0
          {fallback}""",
                      (start, end)).fetchall()
    if not rows:
        return
    wet = _centi(WET_MM_H)
    by_location = {}
    for (location, lead, probability, nearby, field, measured, max_nearby,
         was_then, near_then) in rows:
        stats = by_location.setdefault(location, {}).setdefault(
            _bucket(lead), {'point': [], 'nearby': [], 'field': [0, 0, 0, 0]})
        if probability is not None and measured is not None:
            stats['point'].append((probability / 100, measured >= wet,
                                   None if was_then is None else float(was_then >= wet)))
        if nearby is not None and max_nearby is not None:
            stats['nearby'].append((nearby / 100, max_nearby >= wet,
                                    None if near_then is None else float(near_then >= wet)))
        if field is not None and measured is not None:
            said, was = field >= wet, measured >= wet
            stats['field'][0 if said and was else 1 if was else 2 if said else 3] += 1

    for location in sorted(by_location):
        print(f'\n{location}: probability of rain (≥0.1 mm/h), here and within the '
              'neighbourhood radius', file=out)
        print(f'{"lead":>6} {"n":>6} {"rain%":>6} {"BSS":>6} {"vs pers":>7} '
              f'{"near n":>7} {"rain%":>6} {"BSS":>6} {"vs pers":>7} '
              f'{"map hit":>8} {"map FA":>7}', file=out)
        for bucket in sorted(by_location[location]):
            stats = by_location[location][bucket]
            hits, misses, false_alarms, _ = stats['field']
            print(f'{_label(bucket):>6} {len(stats["point"]):>6} '
                  f'{_base_rate(stats["point"])} {_brier_skill(stats["point"])} '
                  f'{_brier_vs_persistence(stats["point"])} '
                  f'{len(stats["nearby"]):>7} {_base_rate(stats["nearby"])} '
                  f'{_brier_skill(stats["nearby"])} '
                  f'{_brier_vs_persistence(stats["nearby"])} '
                  f'{_ratio(hits, hits + misses, width=8)} '
                  f'{_ratio(false_alarms, hits + false_alarms, width=7)}', file=out)


def _num(value, width=5, digits=2):
    return f'{"–":>{width}}' if value is None else f'{value:>{width}.{digits}f}'


def _ratio(top, bottom, width=5, digits=2):
    return f'{top / bottom:>{width}.{digits}f}' if bottom else f'{"–":>{width}}'


def _percent_of(part, whole, width=5, digits=0):
    return f'{100 * part / whole:>{width - 1}.{digits}f}%' if whole else f'{"–":>{width}}'


def _base_rate(pairs, width=6):
    if not pairs:
        return f'{"–":>{width}}'
    return f'{100 * sum(o for _, o, _ in pairs) / len(pairs):>{width - 1}.1f}%'


def _brier_skill(pairs, width=6):
    """1 - BS / BS_climatology, where climatology is the sample's own rain rate."""
    if not pairs:
        return f'{"–":>{width}}'
    rate = sum(o for _, o, _ in pairs) / len(pairs)
    reference = rate * (1 - rate)
    if reference == 0:
        return f'{"–":>{width}}'
    brier = sum((p - o) ** 2 for p, o, _ in pairs) / len(pairs)
    return f'{1 - brier / reference:>{width}.2f}'


def _brier_vs_persistence(pairs, width=7):
    """1 - BS / BS_persistence, over the steps where the radar then was known.

    Persistence says 100% if it was raining when the forecast was made and 0%
    if not, so its Brier score is simply how often that turned out wrong.
    """
    paired = [(p, o, then) for p, o, then in pairs if then is not None]
    if not paired:
        return f'{"–":>{width}}'
    reference = sum((then - o) ** 2 for _, o, then in paired) / len(paired)
    if reference == 0:
        return f'{"–":>{width}}'
    brier = sum((p - o) ** 2 for p, o, _ in paired) / len(paired)
    return f'{1 - brier / reference:>{width}.2f}'


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(
        prog='python -m ingestor.verify',
        description='Print forecast skill by lead time from the verification store.')
    parser.add_argument('--dir', default=os.environ.get('VERIFY_DIR', ''),
                        help='directory holding verification.sqlite (default: $VERIFY_DIR)')
    parser.add_argument('--days', type=float, default=None,
                        help='how far back to look (default: 30, or everything with --by month)')
    when = parser.add_mutually_exclusive_group()
    when.add_argument('--month', metavar='YYYY-MM',
                      help='one calendar month in full, instead of the last N days')
    when.add_argument('--by', choices=['month'],
                      help='one summary line per month, to compare months')
    product = parser.add_mutually_exclusive_group()
    product.add_argument('--include-fallback', dest='products', action='store_const',
                         const='all', default='primary',
                         help='also count cycles from the deterministic stand-in')
    product.add_argument('--fallback-only', dest='products', action='store_const',
                         const='fallback',
                         help='only the stand-in, to see what an outage costs')
    args = parser.parse_args(argv)
    if not args.dir:
        parser.error('no directory: pass --dir or set VERIFY_DIR')
    if args.month:
        try:
            _month_range(args.month)
        except (ValueError, OverflowError):
            parser.error(f'--month wants YYYY-MM, not {args.month!r}')
        if args.days is not None:
            parser.error('--month and --days do not combine')
    days = args.days if args.days is not None or args.by or args.month else 30.0
    report(args.dir, days, args.products, month=args.month, by_month=bool(args.by))


if __name__ == '__main__':
    main()
