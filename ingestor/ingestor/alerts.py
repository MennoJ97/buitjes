"""Telling someone rain is coming, without becoming a nuisance.

An alert is only useful if it is rare enough to still mean something. The
forecast is republished every five minutes, so the naive rule — "notify while
rain is in the window" — would fire a dozen times an hour for a single shower.
What is worth a notification is the *edge*: rain appearing in the window when it
was not there a moment ago. Everything in this module exists to find that edge
and then keep quiet until it has genuinely passed.

Three mechanisms, because one is not enough:

* **Latching.** Once a rule fires it is `active`, and an active rule stays
  silent no matter how many cycles keep matching.
* **Hysteresis.** It re-arms only when the window drops clearly below the
  threshold — not merely below it — so a shower hovering either side of 0.5
  mm/h cannot ring twice.
* **A quiet period.** A floor on how often one rule may speak at all, for the
  case the other two did not anticipate.

Evaluation runs in the ingestor because that is where the forecast already is:
no extra service, no second copy of the data, and the alert is considered the
moment a cycle is published rather than on a timer of its own.
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass
from datetime import datetime

import requests

log = logging.getLogger('ingestor.alerts')

DEFAULT_THRESHOLD_MM_H = 0.5
DEFAULT_WITHIN_MINUTES = 60
DEFAULT_QUIET_MINUTES = 60

# A rule re-arms when the window falls to this fraction of its threshold. Below
# 1.0 on purpose: re-arming at exactly the threshold means a shower sitting on
# the line re-arms and re-fires on alternating cycles.
REARM_FRACTION = 0.6

DELIVERY_TIMEOUT_SECONDS = 10

#: Which number in a published series a rule watches.
#:
#: The default is the median, and for a lot of weather that is the right
#: question. It is the wrong one for showers. The median is dry unless half the
#: members put rain on one square kilometre, and members disagree about position
#: long before they disagree about whether it will rain at all — so a shower
#: eight of twenty members drop on your street produces a median of zero and an
#: alert that never fires, no matter how the probability floor is set. Watching
#: `p90`, or the neighbourhood probability directly, asks a question that has an
#: answer in that case.
RATE_METRICS = ('median', 'mean', 'p10', 'p25', 'p75', 'p90')
PROBABILITY_METRICS = ('prob', 'prob_nearby')
METRICS = RATE_METRICS + PROBABILITY_METRICS

#: The series key each metric reads.
_SERIES_KEY = {'prob': 'probability', 'prob_nearby': 'probability_nearby'}


@dataclass(frozen=True)
class Rule:
    """One "tell me when" for one published location."""

    location: str
    metric: str          # which number the threshold applies to; see METRICS
    threshold: float
    within: int          # seconds of lead time to look ahead
    probability: float   # minimum share of members; 0 accepts the metric alone
    quiet: int           # seconds before this rule may fire again

    @property
    def is_probability(self) -> bool:
        return self.metric in PROBABILITY_METRICS

    @property
    def series_key(self) -> str:
        return _SERIES_KEY.get(self.metric, self.metric)

    def value(self, entry: dict) -> float:
        """This rule's number, out of one published timestep.

        Falls back to the point probability for `prob_nearby`, so a document
        written before neighbourhood counting existed still evaluates rather
        than reading as zero and re-arming every latched rule at once.
        """
        if self.metric == 'prob_nearby' and entry.get('probability_nearby') is None:
            return float(entry.get('probability', 0.0))
        return float(entry.get(self.series_key, 0.0))

    @property
    def key(self) -> str:
        """Identity in the state file.

        Includes the thresholds, so editing a rule re-arms it rather than
        inheriting the latch of the rule it replaced — an edited rule is a new
        question, and the reader is entitled to an answer to it.
        """
        return (f'{self.location}:{self.metric}:{self.threshold}:'
                f'{self.within}:{self.probability}')


def parse_rules(raw: str) -> tuple[Rule, ...]:
    """Parse ``ALERT_RULES``: ``name:[metric@]threshold:within[:probability[:quiet]]``.

    Semicolon-separated, matching WIDGET_LOCATIONS. Every field except the name
    may be left empty to take the default, so ``home`` and ``home:::0.4`` are
    both valid.

    The metric rides on the threshold field rather than taking a field of its
    own, because it qualifies that number and belongs beside it: ``p90@0.5``
    reads as "the ninetieth percentile reaching 0.5 mm/h". It also leaves every
    rule written before metrics existed parsing exactly as it did.

    For ``prob`` and ``prob_nearby`` the threshold is a fraction rather than a
    rate — ``home:prob_nearby@0.4:30`` is "when two in five members put rain
    within ten kilometres in the next half hour".
    """
    rules = []
    for chunk in (raw or '').split(';'):
        chunk = chunk.strip()
        if not chunk:
            continue
        parts = [part.strip() for part in chunk.split(':')]
        if len(parts) > 5:
            raise ValueError(f'too many fields in alert rule {chunk!r}')
        name = parts[0]
        if not name:
            raise ValueError(f'alert rule {chunk!r} has no location name')

        def field(index, default, convert):
            if index >= len(parts) or parts[index] == '':
                return default
            return convert(parts[index])

        metric, _, threshold_text = field(1, '', str).rpartition('@')
        metric = metric.strip().lower() or 'median'
        if metric not in METRICS:
            raise ValueError(
                f'alert rule {chunk!r}: unknown metric {metric!r}; '
                f'expected one of {", ".join(METRICS)}'
            )

        try:
            rule = Rule(
                location=name,
                metric=metric,
                threshold=(float(threshold_text) if threshold_text.strip()
                           else DEFAULT_THRESHOLD_MM_H),
                within=int(field(2, DEFAULT_WITHIN_MINUTES, float) * 60),
                probability=field(3, 0.0, float),
                quiet=int(field(4, DEFAULT_QUIET_MINUTES, float) * 60),
            )
        except ValueError as error:
            raise ValueError(f'bad alert rule {chunk!r}: {error}') from error

        if rule.threshold <= 0:
            raise ValueError(f'alert rule {chunk!r} needs a threshold above zero')
        if rule.is_probability and rule.threshold > 1.0:
            raise ValueError(
                f'alert rule {chunk!r}: a {metric} threshold is a fraction, 0 to 1'
            )
        if rule.within <= 0:
            raise ValueError(f'alert rule {chunk!r} needs a lead time above zero')
        if not 0.0 <= rule.probability <= 1.0:
            raise ValueError(f'alert rule {chunk!r}: probability is a fraction, 0 to 1')
        rules.append(rule)
    return tuple(rules)


@dataclass(frozen=True)
class Event:
    """A rule's answer for one cycle: what the window holds."""

    rule: Rule
    onset: int | None       # when it crosses the threshold, or None if it never does
    peak: float             # highest value of the rule's metric in the window
    peak_at: int | None
    probability: float      # share of members at the onset step
    raining_now: bool

    @property
    def matched(self) -> bool:
        return self.onset is not None

    @property
    def peak_text(self) -> str:
        """The peak in the units of whatever the rule is watching."""
        if self.rule.is_probability:
            return f'{round(self.peak * 100)}% of members'
        return f'{self.peak:.1f} mm/h'


def evaluate(document: dict, rule: Rule, now: float) -> Event:
    """Read one published point forecast through one rule.

    Only the window between *now* and *now + within* is considered: rain the
    forecast places beyond the lead time is not yet news, and rain in the past
    is not news at all.
    """
    series = (document.get('precipitation') or {}).get('series') or []
    window = [
        entry for entry in series
        if now - 300 <= entry['t'] <= now + rule.within
    ]
    if not window:
        return Event(rule, None, 0.0, None, 0.0, False)

    # The peak is taken in the rule's own metric, because it is what the
    # hysteresis in `process` compares against the rule's own threshold. Mixing
    # the two - latching on p90 and re-arming on the median - would re-arm a
    # rule while the thing it fired about was still in the window.
    peak_entry = max(window, key=rule.value)
    # The probability floor reads the neighbourhood count where there is one:
    # asked as a secondary gate on a rate threshold, "how likely is this" means
    # "how many members see this shower", not "how many put it on this pixel".
    onset = next(
        (entry for entry in window
         if rule.value(entry) >= rule.threshold
         and _floor_probability(entry) >= rule.probability),
        None,
    )
    return Event(
        rule=rule,
        onset=onset['t'] if onset else None,
        peak=rule.value(peak_entry),
        peak_at=peak_entry['t'],
        probability=_floor_probability(onset) if onset else 0.0,
        raining_now=bool(onset and onset['t'] <= now + 300),
    )


def _floor_probability(entry: dict | None) -> float:
    """The probability a rule's `probability` floor is compared against."""
    if not entry:
        return 0.0
    nearby = entry.get('probability_nearby')
    if nearby is not None:
        return float(nearby)
    return float(entry.get('probability', 1.0))


def describe(event: Event, now: float) -> tuple[str, str]:
    """A title and a body, for whatever ends up displaying them."""
    where = event.rule.location
    peak = event.peak_text
    if event.raining_now:
        return (f'Rain at {where}', f'Raining now, peaking around {peak}.')

    minutes = max(1, round((event.onset - now) / 60))
    clock = datetime.fromtimestamp(event.onset).strftime('%H:%M')
    # Suppressed for a rule already watching a probability: the body would
    # otherwise quote the same percentage twice in one sentence.
    chance = (f' ({round(event.probability * 100)}% of members)'
              if event.rule.probability and not event.rule.is_probability else '')
    return (
        f'Rain at {where} in {minutes} min',
        f'Starting around {clock}, peaking around {peak}{chance}.',
    )


class AlertState:
    """Which rules are latched, persisted so a restart is not a fresh start.

    Without this, every `docker compose up` would re-announce rain that was
    announced an hour ago — and restarts happen at exactly the times someone is
    already looking at the thing.
    """

    def __init__(self, path: str):
        self.path = path
        self.rules: dict[str, dict] = {}
        try:
            with open(path, 'r', encoding='utf-8') as handle:
                stored = json.load(handle)
            self.rules = stored.get('rules', {})
        except FileNotFoundError:
            pass
        except (OSError, ValueError) as error:
            # A corrupt state file must not stop the ingest loop; the cost of
            # starting over is at worst one duplicate notification.
            log.warning('alert state at %s unreadable (%s), starting fresh', path, error)

    def entry(self, rule: Rule) -> dict:
        return self.rules.setdefault(rule.key, {'active': False, 'last_fired': 0})

    def prune(self, rules) -> None:
        """Forget rules that are no longer configured."""
        live = {rule.key for rule in rules}
        for key in [key for key in self.rules if key not in live]:
            del self.rules[key]

    def save(self) -> None:
        payload = json.dumps({'rules': self.rules}).encode()
        directory = os.path.dirname(self.path) or '.'
        try:
            temporary = f'{self.path}.tmp'
            with open(temporary, 'wb') as handle:
                handle.write(payload)
            os.replace(temporary, self.path)
        except OSError as error:
            log.warning('could not save alert state to %s: %s', directory, error)


class Notifier:
    """Delivery to one webhook.

    A webhook rather than a built-in integration with any particular service:
    ntfy, Gotify, Home Assistant, Discord and a two-line script all accept one,
    and none of them need code here. `format` picks between a JSON body and
    ntfy's plain-text-plus-headers convention.
    """

    def __init__(self, url: str, fmt: str = 'json', auth: str = ''):
        self.url = url
        self.format = fmt
        self.auth = auth

    def send(self, event: Event, now: float) -> bool:
        """Deliver one rain alert. Returns whether it was accepted.

        The return value decides whether the rule latches, so a webhook that is
        down means the alert is retried on the next cycle rather than silently
        swallowed.
        """
        title, body = describe(event, now)
        payload = {
            'location': event.rule.location,
            'onset': event.onset,
            'peak_at': event.peak_at,
            'probability': round(event.probability, 3),
            'metric': event.rule.metric,
            'peak': round(event.peak, 3),
            'threshold': event.rule.threshold,
        }
        if not event.rule.is_probability:
            # Kept under their old names as well: a webhook consumer written
            # against the previous payload should not break for a rule that
            # still means exactly what it used to.
            payload['peak_mm_h'] = round(event.peak, 2)
            payload['threshold_mm_h'] = event.rule.threshold
        return self.deliver(title, body, tags='cloud_with_rain', payload=payload)

    def deliver(self, title: str, body: str, tags: str = 'cloud_with_rain',
                payload: dict | None = None) -> bool:
        """Put one message on the wire. Returns whether it was accepted.

        Split from `send` because not everything worth saying is about rain at
        a location — `StallWatch` reports the absence of data and has neither.
        """
        headers = {}
        if self.auth:
            headers['Authorization'] = self.auth

        try:
            if self.format == 'ntfy':
                headers.update({
                    'Title': title,
                    'Priority': 'default',
                    'Tags': tags,
                })
                response = requests.post(
                    self.url, data=body.encode('utf-8'), headers=headers,
                    timeout=DELIVERY_TIMEOUT_SECONDS,
                )
            else:
                response = requests.post(
                    self.url,
                    json={'title': title, 'message': body, **(payload or {})},
                    headers=headers,
                    timeout=DELIVERY_TIMEOUT_SECONDS,
                )
            response.raise_for_status()
            return True
        except requests.RequestException as error:
            log.warning('alert delivery failed (%s)', error)
            return False


class StallWatch:
    """Has the upstream gone quiet?

    Separate from the rain rules on purpose. It fires on the *absence* of data,
    it has no location to name, and it has to work for a deployment that
    configured a webhook without ever asking to be told about rain — so it is
    built from ALERT_WEBHOOK_URL alone, not from ALERT_RULES.

    This became necessary when the container healthcheck stopped reading
    /healthz. That was the wrong place to notice a stall — it answered by
    deleting the site from the reverse proxy — but it was the only place, and
    removing it would otherwise mean nothing at all says KNMI has stopped.

    Latched like a rule, and for the same reason, with one deliberate
    difference: the latch closes on the transition whether or not delivery
    succeeded. A stall lasts hours, and the retry-every-cycle behaviour that is
    right for one shower would mean a webhook that is down is re-attempted
    every minute until the weather changes.
    """

    def __init__(self, notifier: Notifier, threshold: int):
        self.notifier = notifier
        self.threshold = threshold
        self.last_cycle: float | None = None
        self.alerted = False

    @classmethod
    def from_config(cls, config) -> 'StallWatch | None':
        if not config.alert_webhook or config.stall_alert <= 0:
            return None
        notifier = Notifier(config.alert_webhook, config.alert_format, config.alert_auth)
        return cls(notifier, config.stall_alert)

    def cycle(self, now: float) -> None:
        """A new forecast cycle landed. Also how the clock is started at boot,
        so a process that never manages to ingest anything still reports it."""
        if self.alerted:
            quiet = int(now - (self.last_cycle or now))
            log.info('forecast cycles resumed after %ds', quiet)
            self.notifier.deliver(
                'Forecast cycles resumed',
                f'A new cycle landed after {quiet // 60} min of silence.',
                tags='white_check_mark',
                payload={'quiet_seconds': quiet},
            )
            self.alerted = False
        self.last_cycle = now

    def check(self, now: float) -> None:
        """Called every time round the ingest loop; speaks once per stall."""
        if self.alerted or self.last_cycle is None:
            return
        quiet = int(now - self.last_cycle)
        if quiet < self.threshold:
            return
        self.alerted = True
        log.warning('no new forecast cycle for %ds; alerting', quiet)
        self.notifier.deliver(
            'Forecast has stalled',
            f'No new forecast cycle for {quiet // 60} min. The map is still '
            f'serving the frames it has; only the data is old.',
            tags='warning',
            payload={'quiet_seconds': quiet},
        )


class AlertRunner:
    """Rules, latch state and delivery as one collaborator.

    Bundled so the ingest path threads a single optional object instead of
    three, and so `None` cleanly means "alerts are not configured".
    """

    def __init__(self, rules, state: AlertState, notifier: Notifier | None):
        self.rules = rules
        self.state = state
        self.notifier = notifier
        self.state.prune(rules)

    @classmethod
    def from_config(cls, config) -> 'AlertRunner | None':
        if not config.alert_rules:
            return None
        notifier = None
        if config.alert_webhook:
            notifier = Notifier(config.alert_webhook, config.alert_format, config.alert_auth)
        else:
            # Worth saying out loud: rules with nowhere to deliver still latch
            # and log, which is useful for a dry run and useless as an alarm.
            log.warning('alert rules are configured but ALERT_WEBHOOK_URL is not; '
                        'alerts will be logged only')
        return cls(config.alert_rules, AlertState(config.alert_state_file), notifier)

    def consider(self, document: dict) -> list[Event]:
        """Evaluate one published document. Never raises: an alert is not worth
        losing a forecast cycle over."""
        try:
            return process(document, self.rules, self.state, self.notifier)
        except Exception:  # noqa: BLE001
            log.exception('alert evaluation failed')
            return []

    def save(self) -> None:
        self.state.save()


def process(document: dict, rules, state: AlertState, notifier: Notifier | None,
            now: float | None = None) -> list[Event]:
    """Evaluate every rule for one location's document; fire what has changed.

    Returns the events that were actually delivered, which is what the caller
    should log — the ones that matched but stayed quiet are the normal case and
    would drown the log.
    """
    now = time.time() if now is None else now
    location = (document.get('location') or {}).get('name')
    fired = []

    for rule in rules:
        if rule.location != location:
            continue

        event = evaluate(document, rule, now)
        entry = state.entry(rule)

        if not event.matched:
            # Re-arm only once the window is clearly dry, not merely under the
            # threshold, so a shower sitting on the line cannot ring twice.
            if entry['active'] and event.peak < rule.threshold * REARM_FRACTION:
                entry['active'] = False
                log.info('alert rule %s re-armed', rule.key)
            continue

        if entry['active']:
            continue
        if now - entry.get('last_fired', 0) < rule.quiet:
            continue

        if notifier is None or notifier.send(event, now):
            entry['active'] = True
            entry['last_fired'] = int(now)
            entry['last_onset'] = event.onset
            fired.append(event)

    return fired
