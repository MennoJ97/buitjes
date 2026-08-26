"""Tests for the ensemble reduction, the crop it runs inside, and no-data.

Run inside the ingestor image, which has no pytest:

    docker compose run --rm --entrypoint python ingestor -m tests.test_ensemble

What is worth testing here is not "does it produce numbers" but the two claims
the map rests on: that the probability-matched mean keeps the ensemble's own
intensity distribution while keeping the mean's placement, and that a pixel no
radar looked at survives the round trip through the frame format as something
other than dry.
"""

import math
import os
import sys

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from ingestor.alerts import Rule, evaluate, parse_rules  # noqa: E402
from ingestor.blend import probability_matched_mean, reduce_members  # noqa: E402
from ingestor.encode import decode_frame, encode_frame  # noqa: E402
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


print()
if failures:
    print(f'{len(failures)} check(s) failed: {", ".join(failures)}')
    sys.exit(1)
print('all ensemble tests passed')
