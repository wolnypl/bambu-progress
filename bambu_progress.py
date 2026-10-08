#!/usr/bin/env python3
"""Read the current Bambu Lab Cloud print progress over MQTT.

The program is deliberately read-only: it subscribes to printer reports and
requests one full status report (``pushall``).  It does not send any print
control command.
"""

from __future__ import annotations

import json
import logging
import os
import signal
import sys
import threading
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

import paho.mqtt.client as mqtt


LOG = logging.getLogger(__name__)
def required(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise SystemExit(f"Missing required environment variable: {name}")
    return value


class ProgressReader:
    def __init__(self) -> None:
        token = required("BAMBU_ACCESS_TOKEN")
        user_id = required("BAMBU_USER_ID")
        self.device_id = required("BAMBU_DEVICE_ID")
        host = os.getenv("BAMBU_MQTT_HOST", "us.mqtt.bambulab.com")
        port = int(os.getenv("BAMBU_MQTT_PORT", "8883"))

        # Accept either the numeric uid or the full u_<uid> form.
        numeric_user_id = user_id.removeprefix("user_").removeprefix("u_")
        username = f"u_{numeric_user_id}"
        self.report_topic = f"device/{self.device_id}/report"
        self.request_topic = f"device/{self.device_id}/request"
        self.result: dict[str, Any] = {}
        self.connection_error: Optional[str] = None
        self.ready = threading.Event()

        self.client = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2,
            client_id=f"bambu-progress-{uuid.uuid4().hex[:12]}",
            protocol=mqtt.MQTTv311,
        )
        self.client.username_pw_set(username, token)
        self.client.tls_set()
        self.client.on_connect = self.on_connect
        self.client.on_message = self.on_message
        self.client.on_connect_fail = self.on_connect_fail
        self.host = host
        self.port = port

    def on_connect(self, client: mqtt.Client, userdata: Any, flags: Any,
                   reason_code: Any, properties: Any) -> None:
        if reason_code != 0:
            self.connection_error = str(reason_code)
            LOG.error("MQTT connection refused: %s", self.connection_error)
            self.ready.set()
            return

        client.subscribe(self.report_topic, qos=1)
        # Ask for a single complete status snapshot. This is a status request,
        # not a printer-control action.
        client.publish(
            self.request_topic,
            json.dumps({
                "pushing": {
                    "sequence_id": "1",
                    "command": "pushall",
                    "version": 1,
                    "push_target": 1,
                }
            }),
            qos=1,
        )

    def on_connect_fail(self, client: mqtt.Client, userdata: Any) -> None:
        LOG.error("Could not connect to the MQTT broker")
        self.ready.set()

    def on_message(self, client: mqtt.Client, userdata: Any,
                   message: mqtt.MQTTMessage) -> None:
        try:
            payload = json.loads(message.payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            LOG.warning("Ignored an invalid JSON MQTT message")
            return

        report = payload.get("print")
        if not isinstance(report, dict):
            return

        # Cloud reports may be deltas, so retain fields seen in prior messages.
        self.result.update(report)
        if "layer_num" in self.result and "total_layer_num" in self.result:
            self.ready.set()

    def read(self) -> dict[str, Any]:
        timeout_seconds = int(os.getenv("BAMBU_TIMEOUT_SECONDS", "20"))
        self.client.connect(self.host, self.port, keepalive=30)
        self.client.loop_start()
        received = self.ready.wait(timeout=timeout_seconds)
        self.client.loop_stop()
        self.client.disconnect()

        if not received:
            raise TimeoutError(
                f"No complete progress report received within {timeout_seconds} seconds"
            )
        if self.connection_error:
            raise PermissionError(
                "Cloud MQTT rejected the login "
                f"({self.connection_error}). Check token, numeric user ID and broker host."
            )
        if "layer_num" not in self.result or "total_layer_num" not in self.result:
            raise RuntimeError("Connected, but the printer did not return layer data")

        return {
            "device_id": self.device_id,
            "state": self.result.get("gcode_state"),
            "current_layer": self.result["layer_num"],
            "total_layers": self.result["total_layer_num"],
            "progress_percent": self.result.get("mc_percent"),
            "remaining_minutes": self.result.get("mc_remaining_time"),
        }


class HomeAssistantPublisher:
    """Keep a local MQTT session and publish Home Assistant Discovery data."""

    def __init__(self, device_id: str) -> None:
        self.device_id = device_id
        self.host = required("HA_MQTT_HOST")
        self.port = int(os.getenv("HA_MQTT_PORT", "1883"))
        self.discovery_prefix = os.getenv(
            "HA_MQTT_DISCOVERY_PREFIX", "homeassistant"
        ).strip("/")
        self.topic_prefix = os.getenv(
            "HA_MQTT_TOPIC_PREFIX", f"bambu_lab/{device_id}"
        ).strip("/")
        self.connected = threading.Event()
        self.connection_error: Optional[str] = None
        self.needs_discovery = True

        self.client = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2,
            client_id=f"bambu-ha-publisher-{uuid.uuid4().hex[:12]}",
            protocol=mqtt.MQTTv311,
        )
        username = os.getenv("HA_MQTT_USERNAME")
        password = os.getenv("HA_MQTT_PASSWORD")
        if username:
            self.client.username_pw_set(username, password)
        if os.getenv("HA_MQTT_TLS", "false").lower() in {"1", "true", "yes"}:
            self.client.tls_set()
        self.client.on_connect = self.on_connect
        self.client.on_disconnect = self.on_disconnect
        self.client.will_set(self.availability_topic, "offline", qos=1, retain=True)
        self.client.reconnect_delay_set(min_delay=1, max_delay=60)

    def on_connect(self, client: mqtt.Client, userdata: Any, flags: Any,
                   reason_code: Any, properties: Any) -> None:
        if reason_code != 0:
            self.connection_error = str(reason_code)
            self.connected.set()
            return
        self.connection_error = None
        self.needs_discovery = True
        self.connected.set()
        client.publish(self.availability_topic, "online", qos=1, retain=True)

    def on_disconnect(self, client: mqtt.Client, userdata: Any,
                      disconnect_flags: Any, reason_code: Any,
                      properties: Any) -> None:
        self.connected.clear()

    @property
    def availability_topic(self) -> str:
        return f"{self.topic_prefix}/availability"

    def connect(self) -> None:
        self.client.connect(self.host, self.port, keepalive=30)
        self.client.loop_start()
        if not self.connected.wait(timeout=10):
            raise TimeoutError("Timed out while connecting to the Home Assistant MQTT broker")
        if self.connection_error:
            raise PermissionError(
                f"Home Assistant MQTT broker rejected the login ({self.connection_error})"
            )
        self.connected.set()

    @property
    def state_topic(self) -> str:
        return f"{self.topic_prefix}/state"

    def device_metadata(self) -> dict[str, Any]:
        return {
            "identifiers": [f"bambu_lab_{self.device_id}"],
            "name": "Bambu Lab A1",
            "manufacturer": "Bambu Lab",
            "model": "A1",
            "serial_number": self.device_id,
        }

    def discovery_payloads(self) -> dict[str, dict[str, Any]]:
        device = self.device_metadata()
        base = {
            "state_topic": self.state_topic,
            "availability_topic": self.availability_topic,
            "device": device,
        }
        return {
            "current_layer": {
                **base,
                "name": "Aktualna warstwa",
                "unique_id": f"bambu_{self.device_id}_current_layer",
                "value_template": "{{ value_json.current_layer }}",
                "icon": "mdi:layers",
                "state_class": "measurement",
            },
            "total_layers": {
                **base,
                "name": "Wszystkie warstwy",
                "unique_id": f"bambu_{self.device_id}_total_layers",
                "value_template": "{{ value_json.total_layers }}",
                "icon": "mdi:layers-triple",
                "entity_category": "diagnostic",
            },
            "progress": {
                **base,
                "name": "Postęp druku",
                "unique_id": f"bambu_{self.device_id}_progress",
                "value_template": "{{ value_json.progress_percent }}",
                "unit_of_measurement": "%",
                "icon": "mdi:progress-clock",
                "state_class": "measurement",
            },
            "remaining_time": {
                **base,
                "name": "Pozostały czas",
                "unique_id": f"bambu_{self.device_id}_remaining_time",
                "value_template": "{{ value_json.remaining_minutes }}",
                "unit_of_measurement": "min",
                "device_class": "duration",
                "icon": "mdi:timer-outline",
            },
            "print_state": {
                **base,
                "name": "Stan druku",
                "unique_id": f"bambu_{self.device_id}_print_state",
                "value_template": "{{ value_json.state }}",
                "icon": "mdi:printer-3d",
            },
            "last_update": {
                **base,
                "name": "Ostatnia aktualizacja",
                "unique_id": f"bambu_{self.device_id}_last_update",
                "value_template": "{{ value_json.last_update }}",
                "device_class": "timestamp",
                "entity_category": "diagnostic",
            },
        }

    def publish_message(self, topic: str, payload: dict[str, Any]) -> None:
        if not self.connected.is_set():
            raise ConnectionError("Home Assistant MQTT broker is not connected")
        result = self.client.publish(topic, json.dumps(payload), qos=1, retain=True)
        result.wait_for_publish()
        if result.rc != mqtt.MQTT_ERR_SUCCESS:
            raise RuntimeError(f"Could not publish to {topic}: MQTT error {result.rc}")

    def publish(self, progress: dict[str, Any]) -> None:
        if self.needs_discovery:
            for sensor, payload in self.discovery_payloads().items():
                topic = f"{self.discovery_prefix}/sensor/bambu_{self.device_id}/{sensor}/config"
                self.publish_message(topic, payload)
            self.needs_discovery = False

        state = {
            **progress,
            "last_update": datetime.now(timezone.utc).isoformat(),
        }
        self.publish_message(self.state_topic, state)

    def close(self) -> None:
        if self.connected.is_set():
            result = self.client.publish(
                self.availability_topic, "offline", qos=1, retain=True
            )
            result.wait_for_publish()
            self.client.loop_stop()
            self.client.disconnect()


def refresh_seconds() -> int:
    value = int(os.getenv("REFRESH_SECONDS", "0"))
    if value < 0:
        raise ValueError("REFRESH_SECONDS must be zero (one run) or a positive number")
    return value


def main() -> int:
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "WARNING"))
    interval = refresh_seconds()
    stop_requested = threading.Event()

    def request_stop(signum: int, frame: Any) -> None:
        LOG.info("Stopping after receiving signal %s", signum)
        stop_requested.set()

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    publisher = HomeAssistantPublisher(required("BAMBU_DEVICE_ID"))
    try:
        publisher.connect()
        while not stop_requested.is_set():
            try:
                progress = ProgressReader().read()
                publisher.publish(progress)
                print(json.dumps(progress, ensure_ascii=False), flush=True)
            except Exception as error:
                LOG.error("Could not update Bambu Cloud progress: %s", error)
                if interval == 0:
                    return 1

            if interval == 0:
                return 0
            stop_requested.wait(interval)
        return 0
    finally:
        publisher.close()


if __name__ == "__main__":
    sys.exit(main())
