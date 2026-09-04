"""Tests for the measured hour a point document carries in front of its forecast.

Run inside the ingestor image, which has no pytest:

    docker compose run --rm --entrypoint python ingestor -m tests.test_measured_history

The forecast half of a point document and its measured half are written at
different moments, out of different things, and what is worth testing is the
seam between them: that the measured steps stop exactly where the forecast
starts, that a pixel no radar looked at becomes a gap rather than a dry step,
that rewriting a document does not stack a second copy of the hour onto it, and
that a document from a cycle that has already expired is left alone.

The pixel arithmetic is tested against fixed answers rather than against itself,
because the number that matters is the one a *browser* reads off the same frame:
`TargetGrid.pixel_for` and `_pixelFor` in the web app's radar.js have to agree,
or the chart and the map describe two different points.
"""

import json
import os
import sys
import tempfile

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from types import SimpleNamespace  # noqa: E402

from ingestor import main as m  # noqa: E402
from ingestor.encode import encode_frame, sample_frame  # noqa: E402
from ingestor.points import Location  # noqa: E402
from ingestor.raster import TargetGrid  # noqa: E402

#: 5-minute aligned, as every reference time from KNMI is.
REF = 1_788_500_400
GRID = TargetGrid(west=3.0, east=8.0, south=50.0, north=54.0, width=100, height=80)
UTRECHT = Location(name='utrecht', lat=52.09, lon=5.12)
#: Inside the forecast domain, outside the crop that gets published.
FARAWAY = Location(name='faraway', lat=60.0, lon=5.0)

failures = []


def check(name, condition, detail=''):
    if condition:
        print(f'  ok   {name}')
    else:
        print(f'  FAIL {name} {detail}')
        failures.append(name)


def config(frame_dir, history_minutes=60):
    """Only the fields these two functions actually read."""
    return SimpleNamespace(frame_dir=frame_dir, history_minutes=history_minutes,
                           max_precip=100.0, widget_locations=(UTRECHT, FARAWAY))


def write_observed(cfg, valid_time, rate, measured=True):
    field = np.zeros((GRID.height, GRID.width), dtype=np.float32)
    mask = np.ones((GRID.height, GRID.width), dtype=bool)
    row, column = GRID.pixel_for(UTRECHT.lon, UTRECHT.lat)
    field[row, column] = rate
    mask[row, column] = measured
    m.write_atomic(cfg.frame_dir, m.observed_frame_name(valid_time),
                   encode_frame(field, cfg.max_precip, mask))


def write_document(cfg, reference_time=REF, name='utrecht'):
    document = {
        'generated_at': REF, 'reference_time': reference_time,
        'location': {'name': name, 'lat': UTRECHT.lat, 'lon': UTRECHT.lon},
        'precipitation': {'unit': 'mm/h', 'series': [
            {'t': reference_time + 300, 'median': 0.1, 'field': 0.2, 'nearby_median': 0.3},
            {'t': reference_time + 600, 'median': 0.4, 'field': 0.5, 'nearby_median': 0.6},
        ]},
        'summary': {'text': 'unchanged'},
    }
    m.write_atomic(cfg.frame_dir, m.point_file_name(name), json.dumps(document).encode())


def read_document(cfg, name='utrecht'):
    with open(os.path.join(cfg.frame_dir, m.point_file_name(name))) as handle:
        return json.load(handle)


# ------------------------------------------------------- the pixel, both ways

print('pixel lookup')
# Answers taken from `_pixelFor` in radar.js for the same grid, as (row, column).
# A browser reading one of these frames at one of these coordinates has to land
# on the same pixel this does.
for lon, lat, expected in [
        (5.5, 52.0, (40, 50)),
        (3.0, 54.0, (0, 0)),          # the north-west corner is pixel zero
        (7.999, 50.001, (79, 99)),    # and the far corner the last one
        (5.12, 52.09, (39, 42)),
        (4.9, 52.37, (33, 38)),
        (2.9, 52.0, None),            # west of the domain
        (8.0, 52.0, None),            # the eastern edge belongs to the next grid
        (5.0, 60.0, None)]:
    got = GRID.pixel_for(lon, lat)
    check(f'{lon},{lat} -> {expected}', got == expected, f'(got {got})')

print()
print('frame sampling')
field = np.zeros((GRID.height, GRID.width), dtype=np.float32)
mask = np.ones((GRID.height, GRID.width), dtype=bool)
field[40, 50] = 3.7
mask[10, 10] = False
frame = encode_frame(field, 100.0, mask)
wet, dry, unmeasured, off = sample_frame(frame, 100.0, [(40, 50), (0, 0), (10, 10), (999, 0)])
check('a wet pixel round-trips', abs(wet - 3.7) < 0.01, f'(got {wet})')
check('a dry pixel is zero, not absent', dry == 0.0, f'(got {dry})')
check('an unmeasured pixel is absent, not dry', unmeasured is None, f'(got {unmeasured})')
check('a pixel off the frame is absent', off is None, f'(got {off})')

# ---------------------------------------------------------------- the history

print()
print('the hour behind the forecast')
with tempfile.TemporaryDirectory() as scratch:
    cfg = config(scratch)
    for step in range(12, -1, -1):
        write_observed(cfg, REF - step * 300, 1.0 + step * 0.1)
    write_observed(cfg, REF - 3900, 9.9)                  # older than the window
    write_observed(cfg, REF - 1800, 5.0, measured=False)  # a hole in the composite
    write_document(cfg)

    history = m.measured_history(cfg, GRID, REF)
    check('a location off the published crop is skipped', sorted(history) == ['utrecht'],
          f'(got {sorted(history)})')
    steps = history['utrecht']
    check('the window is an hour, not what happens to be on disk',
          [step['t'] for step in steps][0] == REF - 3600, f'(got {steps[0]["t"]})')
    check('it ends on the reference time', steps[-1]['t'] == REF, f'(got {steps[-1]["t"]})')
    check('an unmeasured step is a gap, not a dry step',
          len(steps) == 12 and all(step['t'] != REF - 1800 for step in steps),
          f'(got {len(steps)} steps)')
    check('every step carries a measurement and nothing else',
          all(set(step) == {'t', 'measured'} for step in steps))

    m.publish_measured_history(cfg, GRID, REF)
    series = read_document(cfg)['precipitation']['series']
    check('the measured half comes first', 'measured' in series[0])
    check('and stops where the forecast starts',
          series[11]['t'] == REF and series[12]['t'] == REF + 300,
          f'(got {series[11]["t"]} then {series[12]["t"]})')
    check('the forecast half is untouched',
          series[12:] == [{'t': REF + 300, 'median': 0.1, 'field': 0.2, 'nearby_median': 0.3},
                          {'t': REF + 600, 'median': 0.4, 'field': 0.5, 'nearby_median': 0.6}])
    check('and so is everything else in the document',
          read_document(cfg)['summary'] == {'text': 'unchanged'})

    m.publish_measured_history(cfg, GRID, REF)
    m.publish_measured_history(cfg, GRID, REF)
    check('rewriting does not stack a second hour on',
          len(read_document(cfg)['precipitation']['series']) == 14,
          f'(got {len(read_document(cfg)["precipitation"]["series"])})')

    # A radar frame arrives; the window slides with it.
    write_observed(cfg, REF + 300, 2.5)
    write_document(cfg, reference_time=REF + 300)
    m.publish_measured_history(cfg, GRID, REF + 300)
    measured = [step for step in read_document(cfg)['precipitation']['series']
                if 'measured' in step]
    check('the window slides off the back', measured[0]['t'] == REF + 300 - 3600,
          f'(got {measured[0]["t"]})')
    check('and onto the newest frame', measured[-1]['t'] == REF + 300,
          f'(got {measured[-1]["t"]})')

print()
print('documents this must not touch')
with tempfile.TemporaryDirectory() as scratch:
    cfg = config(scratch)
    write_observed(cfg, REF, 1.0)
    write_document(cfg, reference_time=REF - 3600)
    m.publish_measured_history(cfg, GRID, REF)
    check('a document from an expired cycle is left alone',
          [step['t'] for step in read_document(cfg)['precipitation']['series']]
          == [REF - 3300, REF - 3000])

with tempfile.TemporaryDirectory() as scratch:
    cfg = config(scratch, history_minutes=0)
    write_observed(cfg, REF, 1.0)
    write_document(cfg)
    m.publish_measured_history(cfg, GRID, REF)
    check('history switched off publishes none',
          len(read_document(cfg)['precipitation']['series']) == 2)

with tempfile.TemporaryDirectory() as scratch:
    cfg = config(scratch)
    write_observed(cfg, REF, 1.0)
    m.publish_measured_history(cfg, GRID, REF)
    check('a location with no document yet is survivable', True)

print()
if failures:
    print(f'{len(failures)} failed: {", ".join(failures)}')
    sys.exit(1)
print('all measured-history tests passed')
