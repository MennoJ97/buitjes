"""KNMI Notification Service: let the platform tell us when a file appears.

KNMI's documentation is explicit that repeatedly polling the Open Data API to
discover new files counts as abuse, and points at this service instead. It is
MQTT over WebSockets on port 443, authenticated with an API key as the password
(the username is ignored), with one topic per dataset:

    dataplatform/file/v1/{dataset}/{version}/created

The key is a **separate** credential from the Open Data API key.

A notification here is treated purely as a "something changed" signal: the
ingest cycle then does its own lookup, exactly as it would after a poll. Nothing
depends on the message payload, so a change in its shape cannot break ingestion,
and the service is a drop-in replacement for the *timing* of polls rather than
for the discovery logic.
"""

from __future__ import annotations

import logging
import os
import threading
import uuid

log = logging.getLogger(__name__)

TOPIC_TEMPLATE = 'dataplatform/file/v1/{dataset}/{version}/created'


def _dataset_from_topic(topic: str) -> str | None:
    """dataplatform/file/v1/<dataset>/<version>/created -> <dataset>."""
    parts = topic.split('/')
    return parts[3] if len(parts) > 3 else None

#: The broker identifies a session by client id, so it must be stable across
#: restarts for queued messages to be replayed, and unique per client.
CLIENT_ID_FILE = 'mqtt-client-id'


def stable_client_id(directory: str) -> str:
    path = os.path.join(directory, CLIENT_ID_FILE)
    try:
        with open(path) as handle:
            existing = handle.read().strip()
            if existing:
                return existing
    except OSError:
        pass

    client_id = str(uuid.uuid4())
    try:
        with open(path, 'w') as handle:
            handle.write(client_id)
    except OSError:
        log.warning('could not persist MQTT client id; sessions will not resume')
    return client_id


class NotificationListener:
    """Signals when KNMI publishes a file in any of the given datasets."""

    def __init__(self, api_key: str, topics, client_id: str,
                 host: str = 'mqtt.dataplatform.knmi.nl', port: int = 443):
        import paho.mqtt.client as mqtt  # imported lazily: only needed in this mode

        self._mqtt = mqtt
        self._topics = list(topics)
        self._host = host
        self._port = port
        self._signal = threading.Event()
        self._lock = threading.Lock()
        self._announced: set[str] = set()

        self._client = mqtt.Client(
            callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
            client_id=client_id,
            protocol=mqtt.MQTTv5,
            transport='websockets',
        )
        self._client.username_pw_set(username='', password=api_key)
        self._client.tls_set()
        self._client.on_connect = self._on_connect
        self._client.on_message = self._on_message
        self._client.on_disconnect = self._on_disconnect

    def _on_connect(self, client, userdata, flags, reason_code, properties=None):
        if reason_code != 0:
            log.error('notification service refused the connection: %s', reason_code)
            return
        log.info('connected to the notification service')
        for topic in self._topics:
            # QoS 1 so messages queued while we were away are replayed.
            client.subscribe(topic, qos=1)
            log.info('subscribed to %s', topic)

    def _on_message(self, client, userdata, message):
        # Deliberately does no work and parses no payload: the handler must
        # return promptly so it does not hold up the broker's QoS
        # acknowledgements. The topic alone says which dataset moved, which is
        # enough to avoid re-checking the other one.
        log.info('notification on %s', message.topic)
        dataset = _dataset_from_topic(message.topic)
        if dataset:
            with self._lock:
                self._announced.add(dataset)
        self._signal.set()

    def _on_disconnect(self, client, userdata, flags, reason_code, properties=None):
        if reason_code != 0:
            log.warning('notification service disconnected (%s); paho will retry', reason_code)

    def start(self) -> None:
        self._client.reconnect_delay_set(min_delay=1, max_delay=120)
        # clean_start False so the broker keeps our subscriptions and any
        # messages published while the ingestor was down.
        self._client.connect(
            self._host, self._port, keepalive=60,
            clean_start=self._mqtt.MQTT_CLEAN_START_FIRST_ONLY,
        )
        self._client.loop_start()

    def wait(self, timeout: float) -> set[str]:
        """Block until a file is announced.

        Returns the set of dataset names announced since the last call, so the
        caller can look up only what actually moved. An empty set means the
        timeout expired with nothing published.
        """
        self._signal.wait(timeout)
        self._signal.clear()
        with self._lock:
            announced, self._announced = self._announced, set()
        return announced

    def stop(self) -> None:
        self._client.loop_stop()
        try:
            self._client.disconnect()
        except Exception:
            pass
