"""Tests for the ensemble reduction, the crop it runs inside, and no-data.

Run inside the ingestor image, which has no pytest:

    docker compose run --rm --entrypoint python ingestor -m tests.test_ensemble

What is worth testing here is not "does it produce numbers" but the two claims
the map rests on: that the probability-matched mean keeps the ensemble's own
intensity distribution while keeping the mean's placement, and that a pixel no
radar looked at survives the round trip through the frame format as something
other than dry. The same goes for the timestep KNMI publishes empty: what is
worth testing is that it is caught, that standing in for it keeps the members
apart, and that a step with nothing to stand in for it disappears rather than
reading as dry.
"""

import io
import math
import os
import sys

import numpy as np
from PIL import Image

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from ingestor.alerts import Rule, evaluate, parse_rules  # noqa: E402
from ingestor.blend import (  # noqa: E402
    cell_reach, estimate_step, is_degenerate, neighbourhood_maximum,
    probability_matched_mean, reduce_members, repaired_steps, spread_fields,
)
from ingestor.encode import (  # noqa: E402
    SPREAD_FLOOR_MM_H, decode_frame, decode_spread_frame, encode_frame,
    encode_spread_frame,
)
from ingestor.points import Location, PointExtractor, summarise  # noqa: E402
from ingestor.raster import MercatorResampler  # noqa: E402

failures = []


def check(name, condition, detail=''):
    if condition:
        print(f'  ok   {name}')
    else:
        print(f'  FAIL {name} {detail}')
        failures.append(name)


def _caught(fn):
    """Whether calling ``fn`` raises ValueError, which is how misuse is refused."""
    try:
        fn()
    except ValueError:
        return True
    return False


def ensemble(members=20, size=64, seed=3):
    """Displaced showers on a dry background - the case the median gets wrong."""
    rng = np.random.default_rng(seed)
    y, x = np.mgrid[0:size, 0:size].astype(np.float32)
    fields = []
    for _ in range(members):
        field = np.zeros((size, size), np.float32)
        for cx, cy, amp in [(20, 22, 30.0), (44, 40, 18.0)]:
            dx, dy = rng.normal(0, 6, 2)
            field += amp * np.exp(-(((x - cx - dx) ** 2 + (y - cy - dy) ** 2) / (2 * 4.0 ** 2)))
        fields.append(np.where(field < 0.05, 0.0, field))
    return np.stack(fields)


print('probability_matched_mean')
ens = ensemble()
pmm = probability_matched_mean(ens)
mean = ens.mean(axis=0)

check('shape matches one member', pmm.shape == ens.shape[1:])
check('total water is the ensemble mean of it',
      math.isclose(float(pmm.sum()), float(ens.sum() / ens.shape[0]), rel_tol=1e-4),
      f'{pmm.sum()} vs {ens.sum() / ens.shape[0]}')

# The point of the exercise: the mean says where, the members say how hard.
order_pmm = np.argsort(pmm, axis=None)
order_mean = np.argsort(mean, axis=None)
check('ranks the same points as the mean does',
      np.array_equal(pmm.ravel()[order_mean], np.sort(pmm, axis=None)))
check('peak beats the mean it was built from', pmm.max() > mean.max() * 1.5,
      f'{pmm.max()} vs {mean.max()}')
check('peak is in the same place as the mean peak',
      np.argmax(pmm) == np.argmax(mean))

# Wet area is the average member's, not the mean's (too wide) or the median's
# (too narrow). Compared against the pooled member distribution directly.
wet_pmm = float((pmm >= 0.1).mean())
wet_members = float((ens >= 0.1).mean())
check('wet area matches the average member',
      abs(wet_pmm - wet_members) < 0.01, f'{wet_pmm:.4f} vs {wet_members:.4f}')
check('wetter than the median field',
      wet_pmm > float((np.median(ens, axis=0) >= 0.1).mean()))

check('a point every member calls dry stays dry',
      float(pmm[ens.max(axis=0) == 0].max(initial=0.0)) == 0.0)
check('nothing negative', pmm.min() >= 0.0)

dry = np.zeros((4, 8, 8), np.float32)
check('an all-dry ensemble reduces to all dry',
      float(probability_matched_mean(dry).max()) == 0.0)

check('reachable through reduce_members',
      np.array_equal(reduce_members(ens, 'pmm'), pmm))
check('pmm is the default reduction', np.array_equal(reduce_members(ens), pmm))


print('MercatorResampler: cropping and rows')
lat = 48.9955 + np.arange(200) * 0.009
lon = -0.00725 + np.arange(180) * 0.0145
full = MercatorResampler(lat, lon)

field = np.arange(200 * 180, dtype=np.float32).reshape(200, 180)
check('crop of an uncropped grid is the whole grid',
      full.crop(field).shape == (200, 180))
check('output is the declared size', full(full.crop(field)).shape == (full.height, full.width))
cropped_resampler = MercatorResampler(lat, lon, bounds=(1.0, 50.0, 4.0, 53.0))
window = cropped_resampler.crop(field)
check('crop is smaller than the source', window.shape < (200, 180) and window.size > 0)
check('cropped output is the declared size',
      cropped_resampler(window).shape == (cropped_resampler.height, cropped_resampler.width))
check('crop keeps the member axis',
      cropped_resampler.crop(np.broadcast_to(field, (5, 200, 180))).shape
      == (5,) + window.shape)
check('an uncropped field is refused rather than silently misread',
      _caught(lambda: cropped_resampler(field)))

# The row mapping must still land where it did before indices were rebased:
# an output row's latitude, resampled from a field that *is* latitude, comes
# back as that latitude.
latitude_field = np.repeat(lat[:, None], 180, axis=1).astype(np.float64)
resampled = cropped_resampler(cropped_resampler.crop(latitude_field))
merc_north = math.log(math.tan(math.pi / 4 + math.radians(cropped_resampler.north) / 2))
merc_south = math.log(math.tan(math.pi / 4 + math.radians(cropped_resampler.south) / 2))
step = (merc_north - merc_south) / cropped_resampler.height
expected = np.degrees(
    2 * np.arctan(np.exp(merc_north - (np.arange(cropped_resampler.height) + 0.5) * step))
    - math.pi / 2
)
check('rows land on their Mercator latitudes',
      np.allclose(resampled[:, 0], expected, atol=1e-4),
      f'max off by {np.abs(resampled[:, 0] - expected).max():.6f} deg')


print('neighbourhood probability')
locations = [Location(name='home', lat=50.0, lon=1.5)]
extractor = PointExtractor(locations, lat, lon, neighbourhood_km=10.0)

row, column = extractor._cells[0]
members = np.zeros((20, 200, 180), np.float32)
# Half the members put a shower 5 km east, the other half 5 km west: nobody
# rains on the cell itself, but every member rains within ten kilometres.
members[:10, row, column + 5] = 2.0
members[10:, row, column - 5] = 2.0
extractor.observe(1_700_000_000, members)
series = extractor.series_for(0)

check('one entry per observed step', len(series) == 1)
check('point probability sees nothing', series[0]['probability'] == 0.0)
check('neighbourhood probability sees every member',
      series[0]['probability_nearby'] == 1.0, series[0])
check('the median is dry, as the point count implies', series[0]['median'] == 0.0)

far = PointExtractor(locations, lat, lon, neighbourhood_km=10.0)
distant = np.zeros((20, 200, 180), np.float32)
distant[:, row + 40, column + 40] = 5.0   # ~55 km away
far.observe(1_700_000_000, distant)
check('rain outside the radius is not counted',
      far.series_for(0)[0]['probability_nearby'] == 0.0)

edge = PointExtractor([Location(name='corner', lat=float(lat[1]), lon=float(lon[1]))],
                      lat, lon, neighbourhood_km=10.0)
edge.observe(1_700_000_000, np.zeros((20, 200, 180), np.float32))
check('a location at the domain edge does not blow up',
      edge.series_for(0)[0]['probability_nearby'] == 0.0)


print('summary: a dry median with a wet neighbourhood')
REF = 1_700_000_000


def step(offset, median, nearby):
    return {'t': REF + offset, 'median': median, 'p90': median,
            'probability': 0.0, 'probability_nearby': nearby}


certain = summarise([step(0, 0.0, 0.0), step(600, 0.0, 1.0)], REF, radius_km=10)
check('a near-certain shower does not read as "probably dry"',
      'probably dry' not in certain['text'].lower(), certain['text'])
check('it names the radius rather than claiming rain here',
      '10 km' in certain['text'] and 'may miss you' in certain['text'], certain['text'])
check('and says it is not raining yet', certain['raining_now'] is False)
check('the chance is available as a number too', certain['chance_nearby'] == 1.0)

maybe = summarise([step(0, 0.0, 0.0), step(600, 0.0, 0.4)], REF, radius_km=10)
check('a middling chance still reads as probably dry',
      maybe['text'].startswith('Probably dry'), maybe['text'])
check('40% is reported as 40%', '40%' in maybe['text'], maybe['text'])

check('without a radius it says "nearby"',
      'nearby' in summarise([step(600, 0.0, 1.0)], REF)['text'])
check('a low chance is still just dry',
      summarise([step(600, 0.0, 0.1)], REF, 10)['text'] == 'Staying dry.')
check('a wet median is unaffected by any of this',
      summarise([step(0, 2.0, 1.0)], REF, 10)['raining_now'] is True)


print('frame encoding: dry is not the same as unmeasured')
values = np.array([[0.0, 1.5], [0.0, 20.0]], np.float32)
measured = np.array([[True, True], [False, True]])
decoded, decoded_measured = decode_frame(encode_frame(values, 100.0, measured), 100.0)

check('rain survives the round trip',
      np.allclose(decoded[[0, 1], [1, 1]], [1.5, 20.0], atol=0.01), decoded)
check('the measured mask survives', np.array_equal(decoded_measured, measured))
check('a measured dry pixel reads as dry', decoded[0, 0] == 0.0 and decoded_measured[0, 0])
check('an unmeasured pixel is flagged, not zeroed into dryness',
      not decoded_measured[1, 0])

_, all_measured = decode_frame(encode_frame(values, 100.0), 100.0)
check('a frame with no mask is measured everywhere', all_measured.all())

# The flag is only readable at all because the frame is opaque: a browser reads
# a pixel back through a premultiplying 2D canvas, which erases the colour of
# anything at alpha zero. A dry pixel is one with a rate of zero, nothing more.
payload = encode_frame(values, 100.0, measured)
pixels = np.array(Image.open(io.BytesIO(payload)).convert('RGBA'))
check('every pixel is opaque, dry and unmeasured alike', (pixels[:, :, 3] == 255).all())
check('and the flag rides on a pixel a canvas can still read',
      pixels[1, 0, 2] == 255 and pixels[1, 0, 3] == 255)
check('a dry pixel is zero rain rather than zero alpha',
      pixels[0, 0, 0] == 0 and pixels[0, 0, 1] == 0 and pixels[0, 0, 3] == 255)

# Frames written before the format changed are still in the volume and in
# browser caches until they age out, so decoding must not depend on alpha.
legacy = np.array(Image.open(io.BytesIO(payload)).convert('RGBA'))
legacy[:, :, 3] = np.where(legacy[:, :, 0] + legacy[:, :, 1] > 0, 255, 0)
buffer = io.BytesIO()
Image.fromarray(legacy, 'RGBA').save(buffer, format='WEBP', lossless=True,
                                     quality=100, method=1, exact=True)
old_decoded, old_measured = decode_frame(buffer.getvalue(), 100.0)
check('a transparent frame from before the change still decodes',
      np.allclose(old_decoded, decoded, atol=0.01) and np.array_equal(old_measured, measured))


print('alert rules: metrics')
check('bare rules still parse as median',
      parse_rules('home:0.5:60')[0].metric == 'median')
check('a metric can be attached to the threshold',
      parse_rules('home:p90@0.8:30')[0].metric == 'p90'
      and parse_rules('home:p90@0.8:30')[0].threshold == 0.8)
check('a probability metric parses',
      parse_rules('home:prob_nearby@0.4:30')[0].is_probability)
check('an unknown metric is refused', _caught(lambda: parse_rules('home:p42@0.5:30')))
check('a probability above 1 is refused',
      _caught(lambda: parse_rules('home:prob@4:30')))
check('the metric is part of the rule identity',
      parse_rules('home:0.5:60')[0].key != parse_rules('home:p90@0.5:60')[0].key)

NOW = 1_700_000_000
document = {
    'location': {'name': 'home'},
    'precipitation': {'series': [
        # A shower eight of twenty members put here: dry median, high p90,
        # and every member wet somewhere nearby.
        {'t': NOW + 600, 'median': 0.0, 'p90': 2.4, 'mean': 0.9,
         'probability': 0.4, 'probability_nearby': 0.95},
    ]},
}
median_rule = Rule('home', 'median', 0.5, 3600, 0.0, 3600)
p90_rule = Rule('home', 'p90', 0.5, 3600, 0.0, 3600)
prob_rule = Rule('home', 'prob_nearby', 0.8, 3600, 0.0, 3600)
point_prob_rule = Rule('home', 'prob', 0.8, 3600, 0.0, 3600)

check('the median rule misses the displaced shower',
      not evaluate(document, median_rule, NOW).matched)
check('a p90 rule catches it', evaluate(document, p90_rule, NOW).matched)
check('a neighbourhood-probability rule catches it',
      evaluate(document, prob_rule, NOW).matched)
check('a point-probability rule still misses it',
      not evaluate(document, point_prob_rule, NOW).matched)
check('the peak is reported in the rule\'s own metric',
      evaluate(document, p90_rule, NOW).peak == 2.4
      and evaluate(document, prob_rule, NOW).peak == 0.95)
check('a probability peak is worded as members, not mm/h',
      evaluate(document, prob_rule, NOW).peak_text == '95% of members')

legacy = {
    'location': {'name': 'home'},
    'precipitation': {'series': [{'t': NOW + 600, 'median': 1.0, 'probability': 0.9}]},
}
check('a document without neighbourhood counts still evaluates',
      evaluate(legacy, median_rule, NOW).matched)
check('and prob_nearby falls back to the point count on it',
      evaluate(legacy, Rule('home', 'prob_nearby', 0.8, 3600, 0.0, 3600), NOW).matched)


print('a dead timestep')


class FakeSource:
    """Just enough of BlendFile for repaired_steps, counting its reads."""

    def __init__(self, steps):
        self._steps = list(steps)
        self.valid_times = [1_700_000_000 + i * 300 for i in range(len(self._steps))]
        self.reads = 0

    def members(self, index):
        self.reads += 1
        return self._steps[index]


def dead_step(size=64, members=20):
    """What KNMI actually publishes: every member the same, one wet stripe."""
    field = np.zeros((size, size), np.float32)
    field[-1, 10:30] = 1.0
    return np.repeat(field[None], members, axis=0)


live = ensemble(size=64)
dead = dead_step()

check('a real ensemble is not mistaken for a dead step', not is_degenerate(live))
check('twenty identical members is a dead step', is_degenerate(dead))
check('a dead step is not merely a dry one', float(dead.max()) > 0)
check('an all-dry ensemble reads as degenerate, and harmlessly so',
      is_degenerate(np.zeros((20, 8, 8), np.float32)))
check('a one-member product is never degenerate',
      not is_degenerate(np.zeros((1, 8, 8), np.float32)))

before, after = ensemble(size=64, seed=1), ensemble(size=64, seed=2)
stood_in = estimate_step(before, after)
check('the stand-in is member-wise, not pooled',
      np.allclose(stood_in[7], (before[7] + after[7]) / 2))
check('and so the members still disagree',
      not is_degenerate(stood_in) and float(stood_in.std(axis=0).max()) > 0)
check('one side alone is used as it stands', estimate_step(before, None) is before)
check('nothing on either side has no answer', estimate_step(None, None) is None)

source = FakeSource([live, before, dead, after, live])
steps = list(repaired_steps(source))
check('every step is published', len(steps) == 5)
check('only the dead one is flagged',
      [flag for _, _, flag in steps] == [False, False, True, False, False])
check('the flagged step is the blend of its neighbours',
      np.allclose(steps[2][1][7], (before[7] + after[7]) / 2))
check('the stamps are untouched',
      [t for t, _, _ in steps] == source.valid_times)
check('each step is read once, the look-ahead reused',
      source.reads == 5, f'{source.reads} reads')

first_dead = list(repaired_steps(FakeSource([dead, live, before])))
check('a dead first step leans on the one after it',
      len(first_dead) == 3 and first_dead[0][2] and np.array_equal(first_dead[0][1], live))
last_dead = list(repaired_steps(FakeSource([live, before, dead])))
check('a dead last step leans on the one before it',
      len(last_dead) == 3 and last_dead[2][2] and np.array_equal(last_dead[2][1], before))

surrounded = list(repaired_steps(FakeSource([live, dead, dead, dead, after])))
stamps = [t for t, _, _ in surrounded]
check('a run of dead steps leans outward, one to each side',
      np.array_equal(surrounded[1][1], live) and np.array_equal(surrounded[2][1], after))
check('the one in the middle, with nothing either side, is dropped rather than dry',
      len(surrounded) == 4 and 1_700_000_000 + 2 * 300 not in stamps)
check('a wholly dead cycle publishes nothing at all',
      list(repaired_steps(FakeSource([dead, dead]))) == [])

flagged = PointExtractor(locations, lat, lon, neighbourhood_km=10.0)
flagged.observe(1_700_000_000, np.zeros((20, 200, 180), np.float32))
flagged.observe(1_700_000_300, np.zeros((20, 200, 180), np.float32), estimated=True)
published = flagged.series_for(0)
check('an ordinary step says nothing about being estimated',
      'estimated' not in published[0])
check('a stood-in step says so', published[1]['estimated'] is True)


print('the spread layer')

# The running maximum has to equal the thing it is an optimisation of.
rng = np.random.default_rng(11)
noisy = rng.random((3, 40, 40)).astype(np.float32)
for radius in (0, 1, 3, 7, 10):
    fast = neighbourhood_maximum(neighbourhood_maximum(noisy, radius, 1), radius, 2)
    slow = np.zeros_like(noisy)
    for y in range(noisy.shape[1]):
        for x in range(noisy.shape[2]):
            window = noisy[:, max(0, y - radius):y + radius + 1,
                           max(0, x - radius):x + radius + 1]
            slow[:, y, x] = window.max(axis=(1, 2))
    check(f'dilation by doubling matches a plain window at r={radius}',
          np.allclose(fast, slow), f'max off by {np.abs(fast - slow).max()}')

check('a zero radius leaves the field alone',
      np.array_equal(neighbourhood_maximum(noisy, 0, 1), noisy))
edge = np.zeros((1, 5, 5), np.float32)
edge[0, 0, 0] = 4.0
check('the domain edge clips rather than wraps',
      neighbourhood_maximum(neighbourhood_maximum(edge, 1, 1), 1, 2)[0, -1, -1] == 0.0)

# Degrees of longitude are shorter than degrees of latitude, more so further north.
# At 52 N a 0.0145 deg longitude cell is 0.994 km against a 1.002 km latitude
# one, so ten kilometres is eleven cells across and ten down.
rows, columns = cell_reach(10.0, 0.009, 0.0145, 52.0)
check('a radius in km becomes a reach in cells', (rows, columns) == (10, 11), (rows, columns))
check('and reaches further east-west at higher latitude',
      cell_reach(10.0, 0.009, 0.0145, 56.0)[1] > columns)
check('no radius means no reach', cell_reach(0.0, 0.009, 0.0145, 52.0) == (0, 0))

# The whole point: a neighbourhood band lifts off dry where a per-cell one cannot.
shower = np.zeros((20, 60, 60), np.float32)
for member in range(20):
    shower[member, 30, 20 + member] = 5.0      # one shower, twenty positions
low, mid, high = spread_fields(shower, 10, 10)
cell_low, cell_mid, cell_high = np.percentile(shower, [10, 50, 90], axis=0)
check('per-cell percentiles are dry at every one of those positions',
      cell_high[30, 20:40].max() == 0.0)
check('the neighbourhood band is not', high[30, 25].item() == 5.0)
# Column 29 is within reach of all twenty positions, column 15 of only six -
# so the median is the line between "most of the ensemble gets here" and not.
check('and its median tracks whether most of the ensemble reaches',
      mid[30, 29].item() == 5.0 and mid[30, 15].item() == 0.0,
      f'{mid[30, 29]} {mid[30, 15]}')
check('the band is ordered low <= mid <= high',
      bool((low <= mid).all() and (mid <= high).all()))

# The log byte has to survive the round trip better than the data it carries.
rates = np.array([[0.0, 0.05, SPREAD_FLOOR_MM_H, 0.3, 1.0, 7.5, 42.0, 100.0]], np.float32)
payload = encode_spread_frame([rates, rates * 0.5, rates * 2], 100.0)
back = decode_spread_frame(payload, 100.0)
check('dry stays dry rather than becoming the floor', back[0][0, 0] == 0.0)
check('below the floor is dry too', back[0][0, 1] == 0.0)
wet = rates[0, 2:]
check('every rate returns within one log step',
      np.all(np.abs(back[0][0, 2:] - wet) / wet < 0.03),
      str(np.abs(back[0][0, 2:] - wet) / wet))
check('all three channels come back in the order they went in',
      np.allclose(back[1][0, 4], 0.5, rtol=0.03) and np.allclose(back[2][0, 4], 2.0, rtol=0.03))
check('a spread frame is opaque, so a 2D canvas can read it back',
      (np.array(Image.open(io.BytesIO(payload)).convert('RGBA'))[:, :, 3] == 255).all())
check('exactly three fields, or it is refused',
      _caught(lambda: encode_spread_frame([rates, rates], 100.0)))


print()
if failures:
    print(f'{len(failures)} check(s) failed: {", ".join(failures)}')
    sys.exit(1)
print('all ensemble tests passed')
