"""Tests for forecast verification: scoring the forecast frames against the radar.

Run inside the ingestor image, which has no pytest:

    docker compose run --rm --entrypoint python ingestor -m tests.test_verify

What is worth testing here is the bookkeeping rather than the arithmetic of
the textbook scores. A pair must be scored exactly once. A forecast frame must
stay on disk until its radar frame turns up, and no longer. A pixel the radar
did not see must count for neither side. And the band check must put the radar
through the same neighbourhood the band was built over, or a calibrated band
would be reported as too narrow.
"""

import io
import os
import sqlite3
import sys
import tempfile
from types import SimpleNamespace

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from ingestor import main as m  # noqa: E402
from ingestor import verify as v  # noqa: E402
from ingestor.encode import encode_frame, encode_spread_frame  # noqa: E402
from ingestor.points import Location  # noqa: E402
from ingestor.raster import TargetGrid  # noqa: E402

REF = 1_788_500_400
GRID = TargetGrid(west=3.0, east=8.0, south=50.0, north=54.0, width=100, height=80)
UTRECHT = Location(name='utrecht', lat=52.09, lon=5.12)
MAX = 100.0

failures = []


def check(name, condition, detail=''):
    if condition:
        print(f'  ok   {name}')
    else:
        print(f'  FAIL {name} {detail}')
        failures.append(name)


def config(frame_dir, verify_dir, locations=(UTRECHT,)):
    return SimpleNamespace(
        frame_dir=frame_dir, verify_dir=verify_dir, history_minutes=60, max_precip=MAX,
        widget_locations=locations, neighbourhood_km=10.0, spread_radius_km=3.0,
        dataset='test-dataset', keep_cycles=3)


def write(cfg, name, payload):
    with open(os.path.join(cfg.frame_dir, name), 'wb') as handle:
        handle.write(payload)


def field(value=0.0, patch=None):
    """A (80, 100) field, ``value`` inside ``patch`` (rows, columns slices)."""
    out = np.zeros((GRID.height, GRID.width), dtype=np.float32)
    if patch is not None:
        out[patch] = value
    return out


def series(reference, leads):
    return [{'t': reference + lead * 60, 'field': 1.234, 'median': 0.5, 'p10': 0.0,
             'p90': 3.0, 'probability': 0.45, 'probability_nearby': 0.8,
             'nearby_p10': 0.2, 'nearby_median': 1.0, 'nearby_p90': 4.0}
            for lead in leads]


def record(cfg, reference, leads, spread=True, fallback=False):
    source = SimpleNamespace(member_count=1 if fallback else 20, is_fallback=fallback)
    meta = {'reference_time': reference, 'reducer': 'pmm',
            'spread': {'radius_km': 3.0, 'downsample': 2} if spread else None}
    frames = [{'t': reference + lead * 60} for lead in leads]
    extractor = SimpleNamespace(locations=[UTRECHT], neighbourhood_km=10.0,
                                series_for=lambda index: series(reference, leads))
    v.record_cycle(cfg, source, meta, frames, extractor)


def test_scores():
    print('field scores')
    rain = (slice(10, 30), slice(20, 50))
    forecast = field(2.0, rain)
    measured = np.ones(forecast.shape, dtype=bool)
    row = v.field_scores(forecast, forecast.copy(), measured)
    wet = 20 * 30
    check('perfect forecast: every wet pixel a hit', row[1:4] == (wet, 0, 0), row[1:4])
    check('perfect forecast: no error', row[12] == 0.0, row[12])
    check('perfect forecast: FSS numerator zero at both scales',
          row[13] == 0.0 and row[15] == 0.0, row[13:17])

    observed = field()
    blind = measured.copy()
    blind[rain] = False
    row = v.field_scores(forecast, observed, blind)
    check('rain where the radar is blind is not a false alarm', row[1:4] == (0, 0, 0), row[1:4])
    check('blind pixels leave the denominator', row[0] == measured.size - wet, row[0])

    shifted = field(2.0, (slice(10, 30), slice(22, 52)))
    row = v.field_scores(forecast, shifted, measured)
    fss5 = 1 - row[13] / row[14]
    fss25 = 1 - row[15] / row[16]
    check('a 2 km shift scores better at 25 km than at 5 km', fss25 > fss5, (fss5, fss25))

    values = np.random.default_rng(1).random((13, 17)).astype(np.float32)
    window = 5
    padded = np.pad(values, 2)
    brute = np.array([[padded[r:r + window, c:c + window].sum() for c in range(17)]
                      for r in range(13)])
    check('box sum matches the brute-force window sum',
          np.allclose(v._box_sum(values, window), brute, atol=1e-5))


def test_pairing_and_retention():
    print('pairing and retention')
    with tempfile.TemporaryDirectory() as frames, tempfile.TemporaryDirectory() as store:
        cfg = config(frames, store)
        rain = (slice(30, 50), slice(40, 70))
        for lead in (5, 10):
            write(cfg, m.forecast_frame_name(REF, REF + lead * 60),
                  encode_frame(field(2.0, rain), MAX))
            write(cfg, m.spread_frame_name(REF, REF + lead * 60),
                  encode_spread_frame([field(1.0, rain)[::2, ::2]] * 3, MAX))
        # A cycle from before verification was switched on: no cycles row.
        write(cfg, m.forecast_frame_name(REF - 300, REF + 300),
              encode_frame(field(), MAX))
        record(cfg, REF, (5, 10))
        write(cfg, m.observed_frame_name(REF + 300), encode_frame(field(2.0, rain), MAX))
        # The radar at the reference time: the same patch, five pixels east.
        # Held still, that is the persistence forecast for every lead.
        then = (slice(30, 50), slice(45, 75))
        write(cfg, m.observed_frame_name(REF), encode_frame(field(2.0, then), MAX))

        forecasts = m.existing_forecast_frames(cfg)
        cutoff = REF - 3600
        before = v.pending(cfg, forecasts, cutoff)
        check('both frames of the tracked cycle wait to be scored',
              before == {(REF, REF + 300), (REF, REF + 600)}, before)

        observed_times = {REF, REF + 300}
        scored = v.verify_observed(cfg, GRID, forecasts, observed_times, m.FRAME_NAMES)
        check('the one observation scores the one frame that predicted it', scored == 1, scored)
        again = v.verify_observed(cfg, GRID, forecasts, observed_times, m.FRAME_NAMES)
        check('a second pass scores nothing again', again == 0, again)

        after = v.pending(cfg, forecasts, cutoff)
        check('only the unobserved frame is still pending', after == {(REF, REF + 600)}, after)
        check('a frame predicting a time past the cutoff is let go',
              v.pending(cfg, forecasts, REF + 900) == set())

        m.prune(cfg, set(), REF + 3600, hold=after)
        left = sorted(os.listdir(frames))
        check('pruning keeps the pending frame and its spread',
              m.forecast_frame_name(REF, REF + 600) in left
              and m.spread_frame_name(REF, REF + 600) in left, left)
        check('pruning removes the scored frame and its spread',
              m.forecast_frame_name(REF, REF + 300) not in left
              and m.spread_frame_name(REF, REF + 300) not in left, left)

        db = sqlite3.connect(os.path.join(store, v.DB_NAME))
        row = db.execute('SELECT lead_min, measured, hits_t1, misses_t1, false_alarms_t1, '
                         'band_pixels FROM field_scores').fetchall()
        check('the score row is at lead 5 with every wet pixel a hit',
              row == [(5, GRID.width * GRID.height, 600, 0, 0, row[0][5])], row)
        check('the band was checked', row[0][5] and row[0][5] > 0, row)
        persistence = db.execute('SELECT p_hits_t1, p_misses_t1, p_false_alarms_t1, '
                                 'p_fss_num_25 > 0 FROM field_scores').fetchone()
        check('persistence of a patch that moved 5 pixels: 500 hits, 100 each way',
              persistence == (500, 100, 100, 1), persistence)

        forecast = db.execute('SELECT field, probability, probability_nearby, nearby_p90 '
                              'FROM point_forecast WHERE lead_min = 5').fetchone()
        check('point forecast stored as hundredths and percent',
              forecast == (123, 45, 80, 400), forecast)
        observed = db.execute('SELECT valid_time, measured, max_spread FROM point_observed '
                              'ORDER BY valid_time').fetchall()
        # Utrecht falls inside the rain patch at REF + 300 and west of it at REF.
        check('the location was observed at both moments, dry then and wet after',
              observed == [(REF, 0, 0), (REF + 300, 200, 200)], observed)

        out = io.StringIO()
        v.report(store, days=36500, out=out)
        text = out.getvalue()
        check('the report has a 5-minute row', '≤5m' in text, text)
        check('the report has the location', 'utrecht' in text, text)
        check('the report compares with persistence', 'pCSI' in text and 'vs pers' in text,
              text)

        out = io.StringIO()
        v.report(store, days=None, by_month=True, out=out)
        month = v.time.strftime('%Y-%m', v.time.localtime(REF))
        text = out.getvalue()
        check('the monthly summary has a line for the month', f'\n{month:>7} ' in text, text)

        out = io.StringIO()
        v.report(store, days=None, month=month, out=out)
        check('one month in full has the lead table', '≤5m' in out.getvalue(), out.getvalue())
        out = io.StringIO()
        v.report(store, days=None, month='1999-01', out=out)
        check('an empty month says so', 'no scored forecasts' in out.getvalue(),
              out.getvalue())


def test_switchover():
    print('primary taking over a reference time from the stand-in')
    with tempfile.TemporaryDirectory() as frames, tempfile.TemporaryDirectory() as store:
        cfg = config(frames, store)
        rain = (slice(30, 50), slice(40, 70))
        valid = REF + 300
        write(cfg, m.observed_frame_name(REF), encode_frame(field(2.0, rain), MAX))
        write(cfg, m.observed_frame_name(valid), encode_frame(field(2.0, rain), MAX))
        # The stand-in puts the patch ten pixels east: 400 hits, 200 each way.
        write(cfg, m.forecast_frame_name(REF, valid),
              encode_frame(field(2.0, (slice(30, 50), slice(50, 80))), MAX))
        # And a step at its own reference time, which is the radar it started
        # from rather than a forecast.
        write(cfg, m.forecast_frame_name(REF, REF), encode_frame(field(2.0, rain), MAX))
        record(cfg, REF, (0, 5, 60), spread=False, fallback=True)

        db = sqlite3.connect(os.path.join(store, v.DB_NAME))
        hits = lambda: db.execute('SELECT hits_t1, band_pixels FROM field_scores').fetchall()
        flag = lambda: db.execute('SELECT fallback FROM cycles').fetchall()

        v.verify_observed(cfg, GRID, m.existing_forecast_frames(cfg), {REF, valid},
                          m.FRAME_NAMES)
        check('the stand-in is scored, with no band, and lead 0 is not',
              hits() == [(400, None)], hits())
        check('lead 0 is not held back from pruning',
              (REF, REF) not in v.pending(cfg, m.existing_forecast_frames(cfg), REF - 3600))
        check('lead 0 is not recorded for the locations', db.execute(
            'SELECT COUNT(*) FROM point_forecast WHERE lead_min = 0').fetchone()[0] == 0)
        # A lead-0 row as the first deployment wrote them: the report skips it.
        db.execute('INSERT INTO field_scores (reference_time, lead_min, scored_at, measured, '
                   'hits_t1, misses_t1, false_alarms_t1) VALUES (?, 0, 0, 1, 1, 0, 0)', (REF,))
        db.commit()

        def text(products):
            out = io.StringIO()
            v.report(store, days=None, products=products, out=out)
            return out.getvalue()
        check('--fallback-only shows the stand-in', '≤5m' in text('fallback')
              and 'stand-in only' in text('fallback'), text('fallback'))
        five = [line for line in text('fallback').splitlines() if line.strip().startswith('≤5m')]
        check('the report leaves out the old lead-0 row', five and five[0].split()[1] == '1',
              five)
        check('the default report leaves the stand-in out',
              'no scored forecasts' in text('primary'), text('primary'))
        check('--include-fallback counts it', '≤5m' in text('all'), text('all'))

        record(cfg, REF, (0, 5, 60), spread=False, fallback=True)
        check('rebuilding the stand-in keeps its scores',
              sorted(hits()) == [(1, None), (400, None)], hits())

        # The primary's first cycle back lands on the same reference time and
        # overwrites the frame. It forecasts the patch exactly.
        write(cfg, m.forecast_frame_name(REF, valid), encode_frame(field(2.0, rain), MAX))
        record(cfg, REF, (5, 10))
        check('the cycle is now the primary', flag() == [(0,)], flag())
        check("the stand-in's scores are dropped, not relabelled", hits() == [], hits())
        leads = [row[0] for row in db.execute(
            'SELECT lead_min FROM point_forecast ORDER BY lead_min')]
        check("the stand-in's hourly lead is gone from the location rows",
              leads == [5, 10], leads)

        scored = v.verify_observed(cfg, GRID, m.existing_forecast_frames(cfg),
                                   {REF, valid}, m.FRAME_NAMES)
        check("the same lead is scored again, from the primary's frame",
              scored == 1 and hits()[0][0] == 600, (scored, hits()))
        check('--fallback-only is empty now', 'no scored forecasts' in text('fallback'),
              text('fallback'))


def test_retention():
    print('radar retention')
    with tempfile.TemporaryDirectory() as frames, tempfile.TemporaryDirectory() as store:
        for name, verify_dir in (('on', store), ('off', '')):
            cfg = config(frames, verify_dir)
            for hours in (3, 7):
                write(cfg, m.observed_frame_name(REF - hours * 3600), encode_frame(field(), MAX))
            m.prune(cfg, set(), REF)
            left = os.listdir(frames)
            kept = m.observed_frame_name(REF - 3 * 3600) in left
            if name == 'on':
                check('verifying keeps the radar from 3 h ago as a persistence forecast', kept)
            else:
                check('without verification the radar from 3 h ago goes', not kept)
            check(f'verification {name}: the radar from 7 h ago goes',
                  m.observed_frame_name(REF - 7 * 3600) not in left, left)
    first, after = v._month_range('2026-12')
    check('December runs to the first of January',
          v.time.localtime(after)[:3] == (2027, 1, 1) and v.time.localtime(first)[:3]
          == (2026, 12, 1))


def test_band():
    print('band calibration')
    with tempfile.TemporaryDirectory() as frames, tempfile.TemporaryDirectory() as store:
        cfg = config(frames, store, locations=())
        spread = {'radius_km': 3.0, 'downsample': 2}
        measured = np.ones((GRID.height, GRID.width), dtype=bool)
        band = field(2.0, (slice(None), slice(None)))[::2, ::2]
        write(cfg, m.spread_frame_name(REF, REF + 300), encode_spread_frame([band] * 3, MAX))

        everywhere = field(2.0, (slice(None), slice(None)))
        inside = v._band_scores(cfg, m.FRAME_NAMES, spread, REF, REF + 300,
                                v.Observation(everywhere, measured, GRID), MAX)
        check('radar equal to the band is neither below nor above it',
              inside[0] > 0 and inside[1] == 0 and inside[4] == 0, inside)

        dry = v._band_scores(cfg, m.FRAME_NAMES, spread, REF, REF + 300,
                                v.Observation(field(), measured, GRID), MAX)
        check('a dry radar under a wet band is below p10 everywhere',
              dry[1] == dry[0] > 0 and dry[4] == 0, dry)

        # A single wet pixel: after the neighbourhood it covers a patch, and
        # every pixel of that patch is above a dry band. The test grid's
        # pixels are 3-5 km, so a 3 km radius rounds to no reach at all; 15 km
        # is one row and two columns either side.
        spread = {'radius_km': 15.0, 'downsample': 2}
        write(cfg, m.spread_frame_name(REF, REF + 600),
              encode_spread_frame([field()[::2, ::2]] * 3, MAX))
        one = field(5.0, (slice(40, 41), slice(50, 51)))
        spot = v._band_scores(cfg, m.FRAME_NAMES, spread, REF, REF + 600,
                                v.Observation(one, measured, GRID), MAX)
        check('one wet radar pixel spreads to its neighbourhood, all above p90',
              spot[0] == 3 * 5 and spot[4] == spot[0], spot)

        blind = measured.copy()
        blind[38:43, 48:53] = False
        hidden = v._band_scores(cfg, m.FRAME_NAMES, spread, REF, REF + 600,
                                v.Observation(one, blind, GRID), MAX)
        check('pixels with an unmeasured neighbour are left out', hidden[0] < spot[0],
              (hidden, spot))


if __name__ == '__main__':
    test_scores()
    test_pairing_and_retention()
    test_switchover()
    test_retention()
    test_band()
    if failures:
        print(f'\n{len(failures)} failed')
        sys.exit(1)
    print('\nall passed')
