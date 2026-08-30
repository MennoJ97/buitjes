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
    pool_members, pooled_axis, pooled_cell_reach, pooled_reach_km,
    probability_matched_mean, reduce_members, repaired_steps, spread_fields,
)
from ingestor.encode import (  # noqa: E402
    SPREAD_FLOOR_MM_H, decode_frame, decode_spread_frame, encode_frame,
    encode_spread_frame,
)
from ingestor.points import (  # noqa: E402
    Location, PointExtractor, current_conditions, summarise,
)
from ingestor.main import published_steps, spread_plan  # noqa: E402
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

def pmm_reference(values):
    """The straightforward reading: sort the whole pool, zeros and all.

    What :func:`probability_matched_mean` used to do, kept here because what it
    does now - sorting only the wet values and leaving the dry ranks at zero -
    is an optimisation whose whole claim is that it changes nothing. Nineteen
    values in twenty are zero on this product, so the claim is worth a test
    rather than a comment.
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


check('bit-identical to sorting the whole pool',
      np.array_equal(pmm, pmm_reference(ens)))
# The block that straddles the wet/dry boundary is the one the shortcut has to
# get right by arithmetic rather than by having the zeros there, and it only
# exists when the wet count is not a whole multiple of the member count.
for wet_cells in (0, 1, 19, 20, 21, 40, 41):
    sparse = np.zeros((20, 8, 8), np.float32)
    sparse.reshape(20, -1)[:, :1] = 0.0
    flat = sparse.reshape(-1)
    flat[:wet_cells] = np.linspace(0.5, 9.0, wet_cells) if wet_cells else 0.0
    check(f'identical with {wet_cells} wet values in the pool',
          np.array_equal(probability_matched_mean(sparse), pmm_reference(sparse)),
          f'{wet_cells}')

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


print('summary: the sentence describes what is drawn')


def drawn_step(offset, field, median, nearby=0.0):
    return {'t': REF + offset, 'field': field, 'median': median, 'p90': max(field, median),
            'probability': 0.0, 'probability_nearby': nearby}


# The case that forced this: at the wettest cell of a real cycle the field ran
# to 141 mm/h while only a fifth of the members were wet, so the median sat at
# zero and the sentence read "Staying dry" over a chart with a spike in it.
downpour = summarise([drawn_step(0, 141.46, 0.0), drawn_step(300, 32.42, 0.0),
                      drawn_step(600, 0.0, 0.0)], REF, 10)
check('a dry median no longer hides a downpour',
      downpour['raining_now'] is True, downpour['text'])
check('and the rate quoted is the one on the map',
      '141 mm/h' in downpour['text'], downpour['text'])
check('the structured peak follows the same number', downpour['peak_mm_h'] == 141.46)

# The spell is the field's spell, so a step the map paints dry ends it.
spell = summarise([drawn_step(0, 1.0, 1.0), drawn_step(300, 2.0, 1.0),
                   drawn_step(600, 0.0, 1.0), drawn_step(900, 3.0, 1.0)], REF, 10)
check('the spell ends where the drawn field goes dry',
      spell['stops_at'] == REF + 600, spell)
check('and the peak comes from that spell, not the later one',
      spell['peak_mm_h'] == 2.0, spell)

# A dry field with a wet neighbourhood is still the case that branch exists for.
nearby_only = summarise([drawn_step(0, 0.0, 0.0, 0.9), drawn_step(300, 0.0, 0.0, 0.9)],
                        REF, 10)
check('a dry cell with showers around it still says so',
      'Showers about' in nearby_only['text'], nearby_only['text'])

check('a series with no field at all still reads off the median',
      summarise([step(0, 2.0, 0.0)], REF, 10)['peak_mm_h'] == 2.0)

# The sentence stops where the map does. The field is published unclipped, the
# frames saturate, and the ramp's top reads "100+" — so a sentence quoting 481
# names a number nothing on screen can be checked against.
capped = summarise([drawn_step(0, 481.1, 0.0)], REF, 10, max_mm_h=100.0)
check('a rate past the frame ceiling reads as "over"',
      'over 100 mm/h' in capped['text'], capped['text'])
check('but the structured peak keeps the real number', capped['peak_mm_h'] == 481.1)
check('a rate inside the ceiling is untouched',
      '4.2 mm/h' in summarise([drawn_step(0, 4.2, 0.0)], REF, 10, max_mm_h=100.0)['text'])
check('and with no ceiling given nothing is capped',
      '481' in summarise([drawn_step(0, 481.1, 0.0)], REF, 10)['text'])

current = current_conditions({
    'generated_at': REF, 'reference_time': REF,
    'location': {'name': 'home', 'lat': 52.0, 'lon': 5.0},
    'summary': {'text': ''},
    'precipitation': {'unit': 'mm/h', 'field_product': 'probability-matched mean',
                      'series': [dict(drawn_step(0, 4.0, 0.0), p10=0.0, p90=1.0)]},
})
check('the headline value is what the map shows', current['precipitation']['value'] == 4.0)
check('with the members median kept beside it, not instead of it',
      current['precipitation']['median'] == 0.0)
check('and it says which number it is',
      current['precipitation']['product'] == 'probability-matched mean')


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
        # every member wet somewhere nearby — and a map painting it at 6 mm/h,
        # because the field is placed by the ensemble mean rather than counted
        # at this one cell.
        {'t': NOW + 600, 'median': 0.0, 'p90': 2.4, 'mean': 0.9, 'field': 6.0,
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
field_rule = Rule('home', 'field', 1.0, 3600, 0.0, 3600)
check('a rule can watch the field the map draws',
      parse_rules('home:field@1.0:60')[0].metric == 'field')
check('and it fires on a shower the median never sees',
      evaluate(document, field_rule, NOW).matched)
check('reading the field rather than the percentiles beside it',
      evaluate(document, field_rule, NOW).peak == 6.0,
      evaluate(document, field_rule, NOW).peak)

check('a document without neighbourhood counts still evaluates',
      evaluate(legacy, median_rule, NOW).matched)
check('and a field rule on one falls back to the median rather than to zero',
      evaluate(legacy, Rule('home', 'field', 0.5, 3600, 0.0, 3600), NOW).matched)
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

# The field the map draws, published beside the percentiles taken from the
# members. They are different estimators and the whole point is that a reader
# gets the same answer from the chart as from the map.
drawn = np.zeros((200, 180), np.float32)
row0, column0 = extractor._cells[0]
drawn[row0, column0] = 7.5
with_field = PointExtractor(locations, lat, lon, neighbourhood_km=10.0)
with_field.observe(1_700_000_000, np.zeros((20, 200, 180), np.float32), field=drawn)
entry = with_field.series_for(0)[0]
check('the drawn field is published under its own name', entry['field'] == 7.5, entry)
check('and does not disturb the members it sits beside',
      entry['median'] == 0.0 and entry['p90'] == 0.0)
bare = PointExtractor(locations, lat, lon, neighbourhood_km=10.0)
bare.observe(1_700_000_000, np.zeros((20, 200, 180), np.float32))
check('a step observed without one says nothing about it',
      'field' not in bare.series_for(0)[0])

# The neighbourhood band at the location, sampled from the same stack the spread
# frames are built from. Uncropped, so it reads at the location's own cell.
banded = PointExtractor(locations, lat, lon, neighbourhood_km=10.0)
stack = np.zeros((20, 200, 180), np.float32)
stack[:, row0, column0] = 2.5
near = spread_fields(stack, 2, 2)
banded.observe(1_700_000_000, stack, field=drawn, nearby=near)
entry = banded.series_for(0)[0]
check('the neighbourhood band is published at the location',
      entry['nearby_p10'] == 2.5 and entry['nearby_median'] == 2.5
      and entry['nearby_p90'] == 2.5, entry)
check('and a step without one says nothing about it',
      'nearby_median' not in with_field.series_for(0)[0])

# The precedence the summary reads, best available first.
check('the summary prefers the neighbourhood median',
      summarise([{'t': REF, 'nearby_median': 3.0, 'field': 9.0, 'median': 0.0,
                  'p90': 9.0, 'probability': 0.0, 'probability_nearby': 0.0}],
                REF, 10)['peak_mm_h'] == 3.0)
check('then the field',
      summarise([{'t': REF, 'field': 9.0, 'median': 0.0, 'p90': 9.0,
                  'probability': 0.0, 'probability_nearby': 0.0}], REF, 10)['peak_mm_h'] == 9.0)

# The field arrives cropped to what is published, while a location's cell is an
# index into the whole KNMI grid, so the offset has to be taken off.
cropped = PointExtractor(locations, lat, lon, neighbourhood_km=10.0,
                         crop_origin=(row0 - 2, column0 - 3))
window = np.zeros((40, 40), np.float32)
window[2, 3] = 4.25
cropped.observe(1_700_000_000, np.zeros((20, 200, 180), np.float32), field=window)
check('a cropped field is read at the location, not at its raw index',
      cropped.series_for(0)[0]['field'] == 4.25, cropped.series_for(0)[0])

outside = PointExtractor(locations, lat, lon, neighbourhood_km=10.0,
                         crop_origin=(row0 + 50, column0 + 50))
outside.observe(1_700_000_000, np.zeros((20, 200, 180), np.float32), field=window)
check('a location outside the published crop gets no field rather than a zero',
      'field' not in outside.series_for(0)[0])


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
print('pooling the spread layer')

block = np.zeros((2, 8, 8), np.float32)
block[0, 3, 5] = 7.0      # one wet cell, deliberately on an odd index
block[1, 2, 2] = 1.5
pooled = pool_members(block, 2)
check('pooling halves both spatial axes and leaves members alone',
      pooled.shape == (2, 4, 4), f'{pooled.shape}')
check('a peak on an odd index survives, which decimation would drop',
      float(pooled[0, 1, 2]) == 7.0, f'{pooled[0, 1, 2]}')
check('the maximum is what carries, not the mean',
      float(pooled.max()) == 7.0 and float(pooled.sum()) == 8.5)
check('a factor of one is the identity', pool_members(block, 1) is block)
odd = np.arange(2 * 7 * 7, dtype=np.float32).reshape(2, 7, 7)
check('a trailing row and column that cannot fill a block are dropped',
      pool_members(odd, 2).shape == (2, 3, 3))

axis = np.array([50.0, 50.01, 50.02, 50.03])
check('a pooled cell centre sits between the two it covers',
      np.allclose(pooled_axis(axis, 2), [50.005, 50.025]), f'{pooled_axis(axis, 2)}')
check('an odd trailing cell is dropped from the axis too, matching the members',
      len(pooled_axis(np.arange(7.0), 2)) == 3)


class Source:
    """Just the two axes spread_plan reads, on KNMI's actual grid."""

    lat = np.linspace(48.991, 56.011, 780)
    lon = np.linspace(-0.0145, 11.2955, 780)


class Settings:
    def __init__(self, radius=3.0, downsample=2):
        self.spread_radius_km = radius
        self.spread_downsample = downsample


source = Source()
full_res = MercatorResampler(source.lat, source.lon)
factor, reach, band = spread_plan(source, full_res, Settings())

check('the layer is pooled', factor == 2)
check('the band is published at half the rain layer\'s size',
      (band.width, band.height) == (full_res.width // 2, full_res.height // 2),
      f'{band.width}x{band.height} vs {full_res.width}x{full_res.height}')
# The whole point of recomputing rather than dividing. On this grid the
# full-resolution reach is (3, 4); halving it by integer division would give
# (1, 2) - a 2 km radius north-south wearing a 3 km label, and anisotropic.
DLAT = float(source.lat[1] - source.lat[0])
DLON = float(source.lon[1] - source.lon[0])
full_reach = cell_reach(3.0, DLAT, DLON, 52.5)
check('the full-resolution reach is the one production reports',
      full_reach == (3, 4), f'{full_reach}')
check('the pooled reach counts the pooling block',
      reach == (1, 1), f'{reach}')
# Two wrong answers this is deliberately not. Halving the full-resolution reach
# gives (1, 2); asking cell_reach for a reach on the doubled cell size gives
# (2, 2), which spends the block's own half-cell twice and covers 5 km.
check('it is neither the halved reach nor cell_reach on the coarser grid',
      reach != (full_reach[0] // 2, full_reach[1] // 2)
      and reach != cell_reach(3.0, DLAT * 2, DLON * 2, 52.5),
      f'{reach}')
covered = pooled_reach_km(reach, DLAT, DLON, 52.5, 2)
fine_covered = pooled_reach_km(full_reach, DLAT, DLON, 52.5, 1)
check('so it covers about the radius asked for',
      all(abs(km - 3.0) < 1.0 for km in covered),
      f'{covered[0]:.2f} x {covered[1]:.2f} km')
check('as close as the full-resolution reach manages, or closer',
      max(abs(km - 3.0) for km in covered) <= max(abs(km - 3.0) for km in fine_covered),
      f'pooled {covered[0]:.2f}x{covered[1]:.2f} vs full '
      f'{fine_covered[0]:.2f}x{fine_covered[1]:.2f}')
check('a zero radius still reaches nowhere', pooled_cell_reach(0, DLAT, DLON, 52.5, 2) == (0, 0))

# Both layers are stretched across one set of corner coordinates, so they have
# to describe the same rectangle or the band sits offset from the rain.
for edge in ('west', 'east', 'south', 'north'):
    check(f'the pooled grid covers the same {edge} edge',
          abs(getattr(band, edge) - getattr(full_res, edge)) < 1e-9,
          f'{getattr(band, edge)} vs {getattr(full_res, edge)}')

check('switching it off publishes the band on the rain layer\'s own grid',
      spread_plan(source, full_res, Settings(downsample=1))[2] is full_res)
check('and no spread radius means no band at all',
      spread_plan(source, full_res, Settings(radius=None)) == (1, None, None))

# What the pooling is actually for: the band it produces has to still be the
# band. Compared against the full-resolution one sampled at the same cells.
shower_members = ensemble(members=20, size=64)
# A toy grid, so work the two reaches out the same way the planner does rather
# than writing them down: the point is that the footprints match, not the digits.
TOY_DLAT, TOY_DLON = 0.009, 0.0145
toy_fine = cell_reach(3.0, TOY_DLAT, TOY_DLON, 52.5)
toy_pooled = pooled_cell_reach(3.0, TOY_DLAT, TOY_DLON, 52.5, 2)
fine = spread_fields(shower_members, *toy_fine)[:, ::2, ::2]
coarse = spread_fields(pool_members(shower_members, 2), *toy_pooled)
check('the pooled band is ordered low <= mid <= high',
      bool((coarse[0] <= coarse[1] + 1e-6).all() and (coarse[1] <= coarse[2] + 1e-6).all()))
wet = fine[0] > 0.1
lifted = float((coarse[0] <= 0.1).mean()) - float((fine[0] <= 0.1).mean())
check('its floor does not lift off dry, which is the failure to avoid',
      lifted > -0.02, f'dry share moved by {lifted * 100:+.1f} points')
# And the shipped-first-time version, kept as the thing that must stay broken:
# cell_reach on the doubled spacing double-counts the block.
naive = spread_fields(pool_members(shower_members, 2),
                      *cell_reach(3.0, TOY_DLAT * 2, TOY_DLON * 2, 52.5))
naive_lift = float((naive[0] <= 0.1).mean()) - float((fine[0] <= 0.1).mean())
check('and the block-blind reach really would have lifted it',
      naive_lift < lifted - 0.01,
      f'block-blind moved it {naive_lift * 100:+.1f} points, '
      f'block-aware {lifted * 100:+.1f}')
# Against a full-resolution band over the *same footprint*, which is the only
# comparison that isolates what pooling does. The fine reach the planner picks
# is not it: cell_reach rounds up, so (3, 4) covers 3.0 x 3.9 km where the
# pooled reach covers 3.0 x 2.95, and a band over a third less area is
# rightly narrower. pool(f) then dilate(r) spans f*r + f/2 either side, so the
# fine reach matching a pooled r is 2r + 1.
matched = tuple(2 * r + 1 for r in toy_pooled)
same_footprint = spread_fields(shower_members, *matched)[:, ::2, ::2]
width_change = float((coarse[2] - coarse[0]).mean()
                     / max((same_footprint[2] - same_footprint[0]).mean(), 1e-9))
# Within a tenth, and the tenth is the block quantisation rather than slack in
# the test. Half-widths match exactly - 3 fine cells either side both ways -
# but the spans cannot: a fine reach of 3 covers 2*3+1 = 7 fine cells,
# symmetric about the cell it describes, while two pooled cells cover
# 2*(2*1+1) = 6 and no even block can straddle a centre cell evenly. One fine
# cell of footprint out of seven is the whole difference, and it is a floor at
# this factor, not a defect.
check('over the same footprint the band comes out within a tenth',
      0.85 < width_change < 1.15, f'width x{width_change:.2f}')
matched_lift = (float((coarse[0] <= 0.1).mean())
                - float((same_footprint[0] <= 0.1).mean()))
check('and its floor sits where the full-resolution one does',
      abs(matched_lift) < 0.02, f'dry share differs by {matched_lift * 100:+.1f} points')
# Worth stating plainly, because it changes what gets published: the band is
# narrower than the one shipping today, and that is the east-west over-reach
# in the full-resolution rounding going away rather than pooling losing
# anything.
against_today = float((coarse[2] - coarse[0]).mean() / max((fine[2] - fine[0]).mean(), 1e-9))
check('narrower than today\'s published band, as the footprints predict',
      0.7 < against_today < 0.95, f'width x{against_today:.2f} vs today')

# The band is read at a location's own cell, which is not the members' cell
# once the layer is pooled.
here = Location(name='here', lat=float(source.lat[400]), lon=float(source.lon[300]))
scaled = PointExtractor([here], source.lat, source.lon, 10.0, nearby_scale=2)
check('a location maps onto the pooled grid, not the full one',
      scaled._nearby_cells[0] == (scaled._cells[0][0] // 2, scaled._cells[0][1] // 2),
      f'{scaled._nearby_cells[0]} from {scaled._cells[0]}')

print()
print('the published cadence')


class Cadence:
    """Just the two settings :func:`published_steps` reads."""

    def __init__(self, full_cadence_minutes=120, tail_step_minutes=10):
        self.full_cadence_minutes = full_cadence_minutes
        self.tail_step_minutes = tail_step_minutes


REFERENCE = 1_700_000_000
# What KNMI publishes: 72 steps, +5 min to +6 h.
CYCLE = [REFERENCE + minute * 60 for minute in range(5, 361, 5)]


def leads(stamps):
    return sorted((t - REFERENCE) // 60 for t in stamps)


def _walk(stamps, selected):
    """Each stamp paired with the last selected stamp before it."""
    last = None
    for stamp in stamps:
        yield stamp, last
        if stamp in selected:
            last = stamp


kept = leads(published_steps(CYCLE, REFERENCE, Cadence()))
check('every five-minute step survives inside the window',
      kept[:24] == list(range(5, 125, 5)), f'{kept[:24]}')
check('and ten-minute steps after it',
      kept[24:] == list(range(130, 361, 10)), f'{kept[24:]}')
check('48 of 72 steps published', len(kept) == 48, f'{len(kept)}')
check('no gap wider than one tail step',
      max(b - a for a, b in zip(kept, kept[1:])) == 10)
check('the boundary itself is kept, and not twice', kept.count(120) == 1)
check('the horizon is not cut short', kept[-1] == 360)

check('a zero tail step publishes every step',
      published_steps(CYCLE, REFERENCE, Cadence(tail_step_minutes=0)) == set(CYCLE))
check('a tail step at the source cadence also publishes every step',
      published_steps(CYCLE, REFERENCE, Cadence(tail_step_minutes=5)) == set(CYCLE))
check('a window past the horizon publishes every step',
      published_steps(CYCLE, REFERENCE, Cadence(full_cadence_minutes=999)) == set(CYCLE))

# A cycle that already has a hole in it - a step nothing could stand in for -
# must not let the tail drift onto a coarser spacing than it was asked for.
gapped = [t for t in CYCLE if (t - REFERENCE) // 60 not in (200, 205, 210)]
gap_selected = published_steps(gapped, REFERENCE, Cadence())
gap_kept = leads(gap_selected)
# The walk cannot fill a hole the source left, so the promise is not a spacing
# it always achieves - it is that it never passes over a step that would have
# made the spacing better. Anything it skips is inside one tail step of the
# step it last kept, and so could only have made the timeline denser than asked.
premature = [
    (t - REFERENCE) // 60 for t, last in _walk(gapped, gap_selected)
    if t not in gap_selected and last is not None and t - last >= 600
]
check('nothing is skipped that would have improved the spacing',
      not premature, f'{premature}')
check('and every kept stamp is one the source actually published',
      set(gap_kept) <= set(leads(gapped)))
check('the hole is reported at its real width, not papered over',
      max(b - a for a, b in zip(gap_kept, gap_kept[1:])) == 25)

# An hourly source, to show the walk is against the stamps and not a modulus
# that happens to suit five-minute steps.
hourly = [REFERENCE + hour * 3600 for hour in range(1, 13)]
check('a coarser source than the tail step is left alone',
      published_steps(hourly, REFERENCE, Cadence()) == set(hourly))

print()
if failures:
    print(f'{len(failures)} check(s) failed: {", ".join(failures)}')
    sys.exit(1)
print('all ensemble tests passed')
