"""Tests for alert rules.

Run inside the ingestor image, which has no pytest:

    docker compose run --rm --entrypoint python ingestor -m tests.test_alerts

The behaviour worth testing is not "does it notice rain" — that is one
comparison — but "does it stay quiet", since the failure mode of an alerting
system is firing twelve times for one shower and being switched off.
"""

import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from ingestor.alerts import (  # noqa: E402
    AlertRunner, AlertState, Rule, describe, evaluate, parse_rules, process,
)

NOW = 1_700_000_000
failures = []


def check(name, condition, detail=''):
    if condition:
        print(f'  ok   {name}')
    else:
        print(f'  FAIL {name} {detail}')
        failures.append(name)


def document(*rates, start=None, probability=1.0, location='home'):
    """A point document with one 5-minute step per rate given."""
    start = NOW if start is None else start
    return {
        'location': {'name': location},
        'precipitation': {
            'series': [
                {'t': start + index * 300, 'median': rate, 'probability': probability}
                for index, rate in enumerate(rates)
            ]
        },
    }


def fresh_state():
    handle, path = tempfile.mkstemp()
    os.close(handle)
    os.unlink(path)
    return AlertState(path), path


print('parse_rules')
rules = parse_rules('home:0.5:60')
check('one rule', len(rules) == 1)
check('threshold', rules[0].threshold == 0.5)
check('within is seconds', rules[0].within == 3600)
check('defaults applied', rules[0].probability == 0.0 and rules[0].quiet == 3600)

check('bare name works', parse_rules('home')[0].threshold == 0.5)
check('empty fields take defaults', parse_rules('home:::0.4')[0].probability == 0.4)
check('two rules', len(parse_rules('home:1:30; work:0.2:120')) == 2)
check('blank is no rules', parse_rules('') == () and parse_rules('  ;  ') == ())

for bad in ('home:0:60', 'home:-1:60', 'home:0.5:0', 'home:0.5:60:1.5', ':0.5', 'a:b'):
    try:
        parse_rules(bad)
        check(f'rejects {bad!r}', False, '(accepted)')
    except ValueError:
        check(f'rejects {bad!r}', True)

print('evaluate')
rule = parse_rules('home:0.5:60')[0]
check('dry window does not match', not evaluate(document(0.0, 0.1, 0.2), rule, NOW).matched)
check('wet window matches', evaluate(document(0.0, 0.0, 0.9), rule, NOW).matched)
check('onset is the first crossing',
      evaluate(document(0.0, 0.9, 2.0), rule, NOW).onset == NOW + 300)
check('peak is the highest median', evaluate(document(0.0, 0.9, 2.0), rule, NOW).peak == 2.0)

# Rain beyond the lead time is not yet news. The boundary itself counts as
# inside: rain at exactly +60 min is rain in the next 60 minutes.
beyond = document(*([0.0] * 13 + [5.0]))
check('past the lead time is ignored', not evaluate(beyond, rule, NOW).matched)
at_boundary = document(*([0.0] * 12 + [5.0]))
check('the lead time boundary is inside', evaluate(at_boundary, rule, NOW).matched)

# ... and rain in the past is not news at all.
history = document(5.0, 0.0, 0.0, start=NOW - 3600)
check('before now is ignored', not evaluate(history, rule, NOW).matched)

probable = parse_rules('home:0.5:60:0.6')[0]
check('below the probability floor does not match',
      not evaluate(document(0.0, 2.0, probability=0.3), probable, NOW).matched)
check('above the probability floor matches',
      evaluate(document(0.0, 2.0, probability=0.8), probable, NOW).matched)

print('describe')
title, body = describe(evaluate(document(0.0, 0.0, 1.4), rule, NOW), NOW)
check('title names the location and lead time', 'home' in title and 'min' in title, title)
check('body carries the peak', '1.4 mm/h' in body, body)
now_title, now_body = describe(evaluate(document(2.0), rule, NOW), NOW)
check('raining now reads differently', 'Raining now' in now_body, now_body)

# Each cycle republishes a forecast covering *its* next hour, so a document has
# to be built relative to the moment it is evaluated. Reusing one fixed document
# at a later `now` would leave all its rain in the past and match nothing —
# which is a property of the test, not of the rule.
def wet_at(when, rate=1.2):
    return document(0.0, 0.0, rate, start=when)


def dry_at(when, rate=0.0):
    return document(rate, rate, rate, start=when)


print('process: firing once per episode')
state, path = fresh_state()
check('fires on the edge', len(process(wet_at(NOW), rules, state, None, NOW)) == 1)
check('silent while it persists',
      process(wet_at(NOW + 300), rules, state, None, NOW + 300) == [])
check('still silent later',
      process(wet_at(NOW + 1800), rules, state, None, NOW + 1800) == [])

print('process: hysteresis')
# Just under the threshold is not "clearly dry", so the rule stays latched.
check('borderline does not re-arm',
      process(dry_at(NOW + 2100, 0.45), rules, state, None, NOW + 2100) == [])
check('still latched', process(wet_at(NOW + 2400), rules, state, None, NOW + 2400) == [])
# Clearly dry re-arms it.
process(dry_at(NOW + 2700), rules, state, None, NOW + 2700)
check('re-armed by a dry window', not state.entry(rules[0])['active'])

print('process: quiet period')
# Re-armed, but the quiet period has not elapsed since the first alert.
check('quiet period holds', process(wet_at(NOW + 2800), rules, state, None, NOW + 2800) == [])
check('fires again after the quiet period',
      len(process(wet_at(NOW + 3700), rules, state, None, NOW + 3700)) == 1)

print('process: delivery failure does not latch')
state2, _ = fresh_state()


class Refusing:
    def send(self, event, now):
        return False


check('no latch when delivery fails',
      process(wet_at(NOW), rules, state2, Refusing(), NOW) == []
      and not state2.entry(rules[0])['active'])


class Accepting:
    def __init__(self):
        self.sent = []

    def send(self, event, now):
        self.sent.append(event)
        return True


accepting = Accepting()
check('retried on the next cycle',
      len(process(wet_at(NOW + 300), rules, state2, accepting, NOW + 300)) == 1
      and len(accepting.sent) == 1)

print('process: other locations are ignored')
state3, _ = fresh_state()
check('rule only matches its own location',
      process(document(0.0, 5.0, location='work'), rules, state3, None, NOW) == [])

print('state: survives a restart')
state4, path4 = fresh_state()
process(wet_at(NOW), rules, state4, None, NOW)
state4.save()
reloaded = AlertState(path4)
check('latch persisted', reloaded.entry(rules[0])['active'] is True)
check('no repeat after restart',
      process(wet_at(NOW + 300), rules, reloaded, None, NOW + 300) == [])
os.unlink(path4)

print('state: a corrupt file does not stop the loop')
handle, path5 = tempfile.mkstemp()
with os.fdopen(handle, 'w') as stream:
    stream.write('{not json')
check('corrupt state starts fresh', AlertState(path5).rules == {})
os.unlink(path5)

print('state: editing a rule re-arms it')
state6, _ = fresh_state()
process(wet_at(NOW), rules, state6, None, NOW)
edited = parse_rules('home:1.5:60')
check('a changed threshold is a new rule',
      not state6.entry(edited[0])['active'])
state6.prune(edited)
check('the old key is pruned', list(state6.rules) == [edited[0].key])

print('delivery: over a real socket')
# The fakes above prove the latch logic; they prove nothing about whether an
# alert actually leaves the process. This posts to a real server on localhost.
import json as _json  # noqa: E402
import threading  # noqa: E402
from http.server import BaseHTTPRequestHandler, HTTPServer  # noqa: E402

from ingestor.alerts import Notifier  # noqa: E402

received = []


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get('Content-Length', 0))
        received.append({
            'path': self.path,
            'body': self.rfile.read(length).decode(),
            'headers': dict(self.headers),
        })
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b'ok')

    def log_message(self, *args):
        pass


server = HTTPServer(('127.0.0.1', 0), Handler)
threading.Thread(target=server.serve_forever, daemon=True).start()
url = f'http://127.0.0.1:{server.server_port}/buitjes'

event = evaluate(wet_at(NOW), rule, NOW)
check('json delivery reports success', Notifier(url, 'json').send(event, NOW) is True)
payload = _json.loads(received[-1]['body'])
check('json body carries the numbers',
      payload['location'] == 'home' and payload['peak_mm_h'] == 1.2
      and payload['onset'] == NOW + 600, payload)

check('ntfy delivery reports success',
      Notifier(url, 'ntfy', 'Bearer tk_test').send(event, NOW) is True)
check('ntfy sends a plain-text body', '1.2 mm/h' in received[-1]['body'], received[-1]['body'])
check('ntfy sets the title header', 'home' in received[-1]['headers'].get('Title', ''))
check('auth header is passed through',
      received[-1]['headers'].get('Authorization') == 'Bearer tk_test')

server.shutdown()
check('an unreachable webhook fails softly', Notifier(url, 'json').send(event, NOW) is False)

print('config: rules must name a published location')
os.environ.update({
    'KNMI_API_KEY': 'x' * 40,
    'WIDGET_LOCATIONS': 'home:52.09:5.12',
    'ALERT_RULES': 'home:0.5:60',
})
sys.modules.pop('ingestor.config', None)
from ingestor.config import Config  # noqa: E402

check('a rule for a published location is accepted', len(Config.from_env().alert_rules) == 1)

os.environ['ALERT_RULES'] = 'nowhere:0.5:60'
try:
    Config.from_env()
    check('a rule for an unknown location is refused', False, '(accepted)')
except SystemExit as error:
    check('a rule for an unknown location is refused', 'nowhere' in str(error), str(error))

os.environ['ALERT_RULES'] = 'home:not-a-number:60'
try:
    Config.from_env()
    check('an unparseable rule is refused at startup', False, '(accepted)')
except SystemExit:
    check('an unparseable rule is refused at startup', True)

print('stall: one alert per stall, one when it clears')
# The failure this guards is the same one the rain rules guard, from the other
# side: an alarm that repeats every minute for a four-hour KNMI outage gets
# muted, and then the next outage goes unnoticed.
from ingestor.alerts import StallWatch  # noqa: E402


class Collecting:
    def __init__(self):
        self.sent = []

    def deliver(self, title, body, tags='cloud_with_rain', payload=None):
        self.sent.append((title, body, tags))
        return True


sent = Collecting()
watch = StallWatch(sent, threshold=1800)

watch.check(NOW)
check('silent before the clock is started', sent.sent == [])

watch.cycle(NOW)
watch.check(NOW + 1799)
check('silent under the threshold', sent.sent == [])

watch.check(NOW + 1800)
check('alerts at the threshold', len(sent.sent) == 1, sent.sent)
check('the alert says the forecast stalled', 'stalled' in sent.sent[0][0].lower(), sent.sent[0])

watch.check(NOW + 3600)
watch.check(NOW + 7200)
check('still exactly one alert for one stall', len(sent.sent) == 1, sent.sent)

watch.cycle(NOW + 7300)
check('a recovery notice on the next cycle', len(sent.sent) == 2, sent.sent)
check('the recovery says how long it was quiet',
      '121 min' in sent.sent[1][1], sent.sent[1])

watch.cycle(NOW + 7600)
watch.check(NOW + 7600)
check('no second recovery, and the latch re-armed', len(sent.sent) == 2, sent.sent)

watch.check(NOW + 7600 + 1800)
check('a later stall alerts again', len(sent.sent) == 3, sent.sent)


class Failing:
    def __init__(self):
        self.attempts = 0

    def deliver(self, title, body, tags='cloud_with_rain', payload=None):
        self.attempts += 1
        return False


# Deliberately unlike a rain rule, which retries. A stall lasts hours, and
# re-attempting a dead webhook every minute until the weather changes is the
# behaviour this module exists to avoid.
failing = Failing()
stubborn = StallWatch(failing, threshold=1800)
stubborn.cycle(NOW)
for minute in range(30, 240, 1):
    stubborn.check(NOW + minute * 60)
check('a failed delivery is not retried every cycle', failing.attempts == 1,
      f'{failing.attempts} attempts')

print('stall: built from the webhook alone, not from the rules')


def reread():
    """Config.from_env against whatever os.environ currently says."""
    sys.modules.pop('ingestor.config', None)
    import importlib
    return importlib.import_module('ingestor.config').Config.from_env()


os.environ.pop('ALERT_RULES', None)
os.environ['ALERT_WEBHOOK_URL'] = 'http://example.invalid/rain'
config = reread()
check('no rain rules still gets a stall watch', StallWatch.from_config(config) is not None)
check('and no alert runner', AlertRunner.from_config(config) is None)
check('with no STALL_WEBHOOK_URL it falls back to the rain one',
      config.stall_webhook == 'http://example.invalid/rain')

# The separation this exists for: a stall goes to its own topic, and the rain
# rules keep theirs. Sharing one URL was the earlier behaviour and is still
# reachable by leaving STALL_WEBHOOK_URL unset, above.
print('stall: its own webhook, format and auth')
os.environ['STALL_WEBHOOK_URL'] = 'http://ntfy/alerts'
os.environ['STALL_ALERT_FORMAT'] = 'ntfy'
os.environ['STALL_WEBHOOK_AUTH'] = 'Bearer tk_stall'
os.environ['ALERT_FORMAT'] = 'json'
os.environ['ALERT_WEBHOOK_AUTH'] = 'Bearer tk_rain'
config = reread()
watch = StallWatch.from_config(config)
check('stall takes its own url', watch.notifier.url == 'http://ntfy/alerts')
check('stall takes its own format', watch.notifier.format == 'ntfy')
check('stall takes its own auth', watch.notifier.auth == 'Bearer tk_stall')
check('the rain settings are untouched',
      config.alert_format == 'json' and config.alert_auth == 'Bearer tk_rain'
      and config.alert_webhook == 'http://example.invalid/rain')

# A webhook for the stall and nothing for the rain: the reader who only wants
# to know when the pipeline dies.
os.environ.pop('ALERT_WEBHOOK_URL', None)
os.environ.pop('ALERT_WEBHOOK_AUTH', None)
config = reread()
check('stall works with no rain webhook at all',
      StallWatch.from_config(config) is not None)

os.environ['STALL_ALERT_FORMAT'] = 'smoke-signal'
try:
    reread()
    check('a bad stall format is refused at startup', False, '(accepted)')
except SystemExit as error:
    check('a bad stall format is refused at startup',
          'STALL_ALERT_FORMAT' in str(error), str(error))
os.environ['STALL_ALERT_FORMAT'] = 'ntfy'

os.environ['STALL_ALERT_SECONDS'] = '0'
check('zero disables it', StallWatch.from_config(reread()) is None)

print()
if failures:
    print(f'{len(failures)} failed: {", ".join(failures)}')
    sys.exit(1)
print('all alert tests passed')
