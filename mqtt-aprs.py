#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""Bridge APRS-IS traffic into MQTT, with optional MQTT-to-APRS transmit.

The bridge keeps one MQTT connection and one APRS-IS connection alive.
Inbound APRS packets are parsed and published under structured MQTT topics.
When transmission is explicitly enabled, a dedicated MQTT topic can submit a
single APRS frame for transmission through the active APRS-IS connection.

Transmit safety rules:
- APRS transmission is disabled by default.
- A verified APRS passcode is required before transmission can be enabled.
- The transmit topic accepts either a raw APRS frame string or JSON with a
    ``packet`` field.
- Outbound packets must be single-line frames whose source callsign matches the
    configured callsign base.
"""

from __future__ import annotations

__Original_Author__ = "Mike Loebl"
__Refactor_Author__ = "John Canty"
__copyright__ = "None"

import argparse
import configparser
import importlib
import json
import logging
import os
import signal
import sys
import time
from dataclasses import dataclass
from math import atan2, cos, radians, sin, sqrt
from pathlib import Path
from threading import Event, Lock
from typing import Any


def load_optional_module(module_name: str) -> tuple[Any, ImportError | None]:
    """Import an optional dependency and capture the import error if missing."""

    try:
        return importlib.import_module(module_name), None
    except ImportError as exc:
        return None, exc


aprslib, APRSLIB_IMPORT_ERROR = load_optional_module("aprslib")
paho, PAHO_IMPORT_ERROR = load_optional_module("paho.mqtt.client")
setproctitle, _ = load_optional_module("setproctitle")


DEFAULT_CONFIG_PATH = Path("/etc/mqtt-aprs/mqtt-aprs.cfg")
LOCAL_CONFIG_PATH = Path(__file__).with_name("mqtt-aprs.cfg")
COMMON_APRS_PORTS = (14580, 10152, 14581)
MQTT_CONNECT_TIMEOUT_SECONDS = 15
MQTT_RETRY_SECONDS = 10
APRS_RETRY_SECONDS = 30
LOGFORMAT = "%(asctime)-15s %(levelname)s %(message)s"


class ConfigError(ValueError):
    """Raised when the configuration file is present but semantically invalid."""

    pass


@dataclass(frozen=True)
class Settings:
    """Normalized runtime configuration loaded from the INI file."""

    debug: bool
    logfile: str
    mqtt_host: str
    mqtt_port: int
    mqtt_subtopic: str
    mqtt_username: str
    mqtt_password: str
    mqtt_tx_enable: bool
    mqtt_tx_topic: str
    metric_units: bool
    aprs_callsign: str
    aprs_password: str
    aprs_host: str
    aprs_port: int
    aprs_filter: str
    aprs_process: bool
    aprs_latitude: float | None
    aprs_longitude: float | None

    @property
    def app_name(self) -> str:
        return self.mqtt_subtopic

    @property
    def mqtt_topic(self) -> str:
        return f"RF/{self.mqtt_subtopic}"

    @property
    def presence_topic(self) -> str:
        return f"{self.mqtt_topic}/state"

    @property
    def mqtt_tx_status_topic(self) -> str:
        return f"{self.mqtt_tx_topic}/status"

    @property
    def aprs_ports_to_try(self) -> tuple[int, ...]:
        ports = [self.aprs_port]
        ports.extend(port for port in COMMON_APRS_PORTS if port != self.aprs_port)
        return tuple(ports)


def normalize_text(value: Any) -> str:
    """Convert a value to trimmed text, treating ``None`` as empty."""

    return str(value).strip() if value is not None else ""


def callsign_base(callsign: str) -> str:
    """Return a callsign without SSID, normalized to upper case."""

    return normalize_text(callsign).split("-", 1)[0].upper()


def parse_legacy_bool(value: Any, *, default: bool) -> bool:
    """Parse booleans from legacy config values such as 0/1, True/False, yes/no."""

    normalized = normalize_text(value).lower()
    if not normalized:
        return default

    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False

    raise ConfigError(f"Invalid boolean value: {value!r}")


def parse_optional_int(value: Any, *, option_name: str, default: int | None = None) -> int | None:
    """Parse an optional integer from config text."""

    normalized = normalize_text(value)
    if not normalized:
        return default

    try:
        return int(normalized)
    except ValueError as exc:
        raise ConfigError(f"Invalid integer for {option_name}: {value!r}") from exc


def parse_optional_float(value: Any, *, option_name: str) -> float | None:
    """Parse an optional floating-point value from config text."""

    normalized = normalize_text(value)
    if not normalized:
        return None

    try:
        return float(normalized)
    except ValueError as exc:
        raise ConfigError(f"Invalid float for {option_name}: {value!r}") from exc


def get_config_value(config: configparser.RawConfigParser, option_name: str, *, default: str = "") -> str:
    """Read a string value from the ``global`` config section."""

    if not config.has_option("global", option_name):
        return default
    return config.get("global", option_name)


def get_required_config_value(config: configparser.RawConfigParser, option_name: str) -> str:
    """Read a required config value and raise a helpful error when it is missing."""

    value = normalize_text(get_config_value(config, option_name))
    if not value:
        raise ConfigError(f"Missing required [global] option {option_name}")
    return value


def resolve_config_path(explicit_path: str | None) -> Path:
    """Resolve the config path from CLI, environment, or default locations."""

    if explicit_path:
        config_path = Path(explicit_path).expanduser()
        if config_path.is_file():
            return config_path
        raise FileNotFoundError(f"Config file not found: {config_path}")

    env_path = os.getenv("MQTT_APRS_CONFIG")
    if env_path:
        config_path = Path(env_path).expanduser()
        if config_path.is_file():
            return config_path
        raise FileNotFoundError(f"Config file not found: {config_path}")

    for config_path in (DEFAULT_CONFIG_PATH, LOCAL_CONFIG_PATH):
        if config_path.is_file():
            return config_path

    raise FileNotFoundError(
        "No config file found. Checked /etc/mqtt-aprs/mqtt-aprs.cfg and a local mqtt-aprs.cfg."
    )


def load_settings(config_path: Path) -> Settings:
    """Load, normalize, and validate runtime settings from an INI file."""

    config = configparser.RawConfigParser()
    if not config.read(config_path):
        raise FileNotFoundError(f"Unable to read config file: {config_path}")

    if not config.has_section("global"):
        raise ConfigError("Config file is missing the [global] section")

    mqtt_subtopic = get_required_config_value(config, "MQTT_SUBTOPIC").strip("/")
    if not mqtt_subtopic:
        raise ConfigError("MQTT_SUBTOPIC must not be empty")

    mqtt_root_topic = f"RF/{mqtt_subtopic}"
    mqtt_tx_enable = parse_legacy_bool(get_config_value(config, "MQTT_TX_ENABLE"), default=False)
    mqtt_tx_topic = normalize_text(get_config_value(config, "MQTT_TX_TOPIC")).strip("/")
    if not mqtt_tx_topic:
        mqtt_tx_topic = f"{mqtt_root_topic}/tx"

    if "+" in mqtt_tx_topic or "#" in mqtt_tx_topic:
        raise ConfigError("MQTT_TX_TOPIC must be a concrete topic and cannot contain MQTT wildcards")

    if mqtt_tx_topic in {mqtt_root_topic, f"{mqtt_root_topic}/state"}:
        raise ConfigError("MQTT_TX_TOPIC must not overlap the base publish topic or presence topic")

    aprs_latitude = parse_optional_float(
        get_config_value(config, "APRS_LATITUDE"),
        option_name="APRS_LATITUDE",
    )
    aprs_longitude = parse_optional_float(
        get_config_value(config, "APRS_LONGITUDE"),
        option_name="APRS_LONGITUDE",
    )

    if (aprs_latitude is None) != (aprs_longitude is None):
        raise ConfigError("APRS_LATITUDE and APRS_LONGITUDE must be set together")

    mqtt_port = parse_optional_int(
        get_config_value(config, "MQTT_PORT"),
        option_name="MQTT_PORT",
        default=1883,
    )
    aprs_port = parse_optional_int(
        get_config_value(config, "APRS_PORT"),
        option_name="APRS_PORT",
        default=COMMON_APRS_PORTS[0],
    )

    if mqtt_port is None or aprs_port is None:
        raise ConfigError("MQTT_PORT and APRS_PORT must resolve to valid integers")

    aprs_password = normalize_text(get_config_value(config, "APRS_PASSWORD", default="-1")) or "-1"
    if mqtt_tx_enable and aprs_password == "-1":
        raise ConfigError("MQTT_TX_ENABLE requires a verified APRS passcode instead of -1")

    return Settings(
        debug=parse_legacy_bool(get_config_value(config, "DEBUG"), default=False),
        logfile=normalize_text(get_config_value(config, "LOGFILE")),
        mqtt_host=get_required_config_value(config, "MQTT_HOST"),
        mqtt_port=mqtt_port,
        mqtt_subtopic=mqtt_subtopic,
        mqtt_username=normalize_text(get_config_value(config, "MQTT_USERNAME")),
        mqtt_password=normalize_text(get_config_value(config, "MQTT_PASSWORD")),
        mqtt_tx_enable=mqtt_tx_enable,
        mqtt_tx_topic=mqtt_tx_topic,
        metric_units=parse_legacy_bool(get_config_value(config, "METRICUNITS"), default=False),
        aprs_callsign=get_required_config_value(config, "APRS_CALLSIGN"),
        aprs_password=aprs_password,
        aprs_host=get_required_config_value(config, "APRS_HOST"),
        aprs_port=aprs_port,
        aprs_filter=normalize_text(get_config_value(config, "APRS_FILTER")),
        aprs_process=parse_legacy_bool(get_config_value(config, "APRS_PROCESS"), default=True),
        aprs_latitude=aprs_latitude,
        aprs_longitude=aprs_longitude,
    )


def configure_logging(settings: Settings) -> None:
    """Configure process logging to stderr and optionally to a file."""

    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stderr)]
    basic_config_kwargs: dict[str, Any] = {
        "format": LOGFORMAT,
        "level": logging.DEBUG if settings.debug else logging.INFO,
        "handlers": handlers,
        "force": True,
    }

    if settings.logfile:
        try:
            handlers.append(logging.FileHandler(settings.logfile, encoding="utf-8"))
        except OSError as exc:
            logging.basicConfig(**basic_config_kwargs)
            logging.warning(
                "Failed to open log file %s: %s. Continuing with stderr logging only.",
                settings.logfile,
                exc,
            )
        else:
            logging.basicConfig(**basic_config_kwargs)
    else:
        logging.basicConfig(**basic_config_kwargs)


def mqtt_error_message(error_code: Any) -> str:
    """Return a human-readable Paho MQTT error string when possible."""

    if paho is None:
        return str(error_code)

    try:
        return paho.error_string(error_code)
    except Exception:
        return str(error_code)


def ensure_runtime_dependencies() -> None:
    """Fail fast with a clear error when runtime dependencies are not installed."""

    missing_packages = []

    if paho is None:
        missing_packages.append(f"paho-mqtt ({PAHO_IMPORT_ERROR})")
    if aprslib is None:
        missing_packages.append(f"aprslib ({APRSLIB_IMPORT_ERROR})")

    if missing_packages:
        raise RuntimeError(
            "Missing required Python packages: " + ", ".join(missing_packages)
        )


def decode_packet(packet: bytes | str) -> str:
    """Decode APRS raw packet bytes using a permissive single-byte codec."""

    if isinstance(packet, bytes):
        return packet.decode("latin-1", errors="replace").strip()
    return str(packet).strip()


def decode_mqtt_payload(payload: bytes) -> str:
    """Decode inbound MQTT payload text as UTF-8 with replacement."""

    return payload.decode("utf-8", errors="replace").strip()


def mqtt_payload(value: Any) -> str:
    """Serialize Python values to MQTT payload text."""

    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace").strip()
    if isinstance(value, dict):
        return json.dumps(value, sort_keys=True)
    if isinstance(value, (list, tuple)):
        return ",".join(str(item) for item in value)
    if isinstance(value, bool):
        return json.dumps(value)
    return str(value).strip()


def reason_code_value(reason_code: Any) -> Any:
    """Normalize Paho reason code objects into simple comparable values."""

    return getattr(reason_code, "value", reason_code)


class MqttAprsBridge:
    """Coordinate MQTT connectivity, APRS-IS consumption, and optional transmit."""

    def __init__(self, settings: Settings):
        """Create the bridge and initialize MQTT state."""

        ensure_runtime_dependencies()
        self.settings = settings
        self.stop_event = Event()
        self.mqtt_connected = Event()
        self.mqtt_connection_state = Event()
        self.mqtt_connect_result: Any = None
        self.mqtt_loop_started = False
        self.aprs_client: Any | None = None
        self.aprs_client_lock = Lock()
        self.mqtt_client = self.build_mqtt_client()

    def build_mqtt_client(self) -> Any:
        """Build and configure the Paho MQTT client instance."""

        client_id = f"{self.settings.app_name}_{os.getpid()}"
        client = paho.Client(
            paho.CallbackAPIVersion.VERSION2,
            client_id=client_id,
            clean_session=True,
            userdata=None,
            protocol=paho.MQTTv311,
            transport="tcp",
        )

        if self.settings.mqtt_username:
            client.username_pw_set(self.settings.mqtt_username, self.settings.mqtt_password or None)

        client.will_set(self.settings.presence_topic, "0", qos=0, retain=True)
        client.reconnect_delay_set(min_delay=5, max_delay=60)
        client.on_connect = self.on_mqtt_connect
        client.on_disconnect = self.on_mqtt_disconnect
        client.on_publish = self.on_mqtt_publish
        client.on_subscribe = self.on_mqtt_subscribe
        client.on_message = self.on_mqtt_message

        if self.settings.debug:
            client.enable_logger(logging.getLogger("paho.mqtt"))

        return client

    def on_mqtt_connect(self, client: Any, userdata: Any, flags: Any, reason_code: Any, properties: Any) -> None:
        """Handle MQTT broker connections and restore subscriptions after reconnect."""

        code = reason_code_value(reason_code)
        self.mqtt_connect_result = code
        self.mqtt_connection_state.set()

        if code == 0:
            self.mqtt_connected.set()
            logging.info("Connected to MQTT %s:%s", self.settings.mqtt_host, self.settings.mqtt_port)
            self.publish_presence(True)

            if self.settings.mqtt_tx_enable:
                result, _mid = client.subscribe(self.settings.mqtt_tx_topic, qos=1)
                if result == paho.MQTT_ERR_SUCCESS:
                    logging.info("Subscribed to outbound APRS MQTT topic %s", self.settings.mqtt_tx_topic)
                else:
                    logging.error(
                        "Failed to subscribe to outbound APRS MQTT topic %s: rc=%s",
                        self.settings.mqtt_tx_topic,
                        result,
                    )
            return

        self.mqtt_connected.clear()
        logging.error("MQTT connection refused with reason code %s", code)

    def on_mqtt_disconnect(
        self,
        client: Any,
        userdata: Any,
        disconnect_flags: Any,
        reason_code: Any,
        properties: Any,
    ) -> None:
        """Track MQTT disconnects so outbound publishes can react accordingly."""

        code = reason_code_value(reason_code)
        self.mqtt_connected.clear()

        if code == 0:
            logging.info("Clean MQTT disconnection")
        else:
            logging.warning(
                "MQTT disconnected unexpectedly with reason code %s. The network loop will retry.",
                code,
            )

    def on_mqtt_publish(self, client: Any, userdata: Any, mid: int, reason_codes: Any, properties: Any) -> None:
        """Log publish acknowledgements at debug level."""

        logging.debug("MQTT message id %s published", mid)

    def on_mqtt_subscribe(
        self,
        client: Any,
        userdata: Any,
        mid: int,
        reason_codes: Any,
        properties: Any,
    ) -> None:
        """Log MQTT subscription acknowledgements for operational visibility."""

        logging.debug("MQTT subscription acknowledged for mid %s", mid)

    def on_mqtt_message(self, client: Any, userdata: Any, msg: Any) -> None:
        """Accept outbound APRS commands from the configured MQTT topic."""

        if msg.topic != self.settings.mqtt_tx_topic:
            logging.debug("Ignoring unexpected MQTT message on topic %s", msg.topic)
            return

        payload_text = decode_mqtt_payload(msg.payload)

        try:
            packet_text = self.extract_tx_packet(payload_text)
            self.send_aprs_packet(packet_text)
        except ValueError as exc:
            logging.warning("Rejected outbound APRS payload from %s: %s", msg.topic, exc)
            self.publish_tx_status(False, payload_text, str(exc))
        except RuntimeError as exc:
            logging.warning("Failed to send outbound APRS packet from %s: %s", msg.topic, exc)
            self.publish_tx_status(False, payload_text, str(exc))
        else:
            logging.info("Sent outbound APRS packet from MQTT topic %s", msg.topic)
            self.publish_tx_status(True, packet_text)

    def connect_mqtt(self) -> None:
        """Connect to the MQTT broker and wait for a successful CONNACK."""

        while not self.stop_event.is_set():
            try:
                logging.info("Connecting to MQTT %s:%s", self.settings.mqtt_host, self.settings.mqtt_port)
                self.mqtt_connected.clear()
                self.mqtt_connection_state.clear()
                self.mqtt_connect_result = None

                if self.mqtt_loop_started:
                    result = self.mqtt_client.reconnect()
                else:
                    result = self.mqtt_client.connect(self.settings.mqtt_host, self.settings.mqtt_port, 60)
                    if result == paho.MQTT_ERR_SUCCESS:
                        self.mqtt_client.loop_start()
                        self.mqtt_loop_started = True

                if result != paho.MQTT_ERR_SUCCESS:
                    logging.warning(
                        "MQTT connect call returned error code %s (%s). Retrying.",
                        result,
                        mqtt_error_message(result),
                    )
                else:
                    # Paho only reports a usable session after the on_connect
                    # callback updates the shared connection state.
                    if not self.mqtt_connection_state.wait(timeout=MQTT_CONNECT_TIMEOUT_SECONDS):
                        logging.warning(
                            "Timed out waiting for MQTT connection acknowledgement after %s seconds",
                            MQTT_CONNECT_TIMEOUT_SECONDS,
                        )
                    elif self.mqtt_connect_result == 0:
                        return
                    elif self.mqtt_connect_result in {1, 2, 4, 5}:
                        raise RuntimeError(
                            f"MQTT connection failed with non-retryable reason code {self.mqtt_connect_result}"
                        )
                    else:
                        logging.warning(
                            "MQTT connection failed with reason code %s. Retrying.",
                            self.mqtt_connect_result,
                        )

                # Clear any partial client state before the next retry attempt.
                try:
                    self.mqtt_client.disconnect()
                except Exception:
                    logging.debug("MQTT disconnect during retry cleanup failed", exc_info=True)
            except OSError as exc:
                logging.warning("MQTT connection attempt failed: %s", exc)
            except RuntimeError:
                raise

            if self.stop_event.wait(MQTT_RETRY_SECONDS):
                break

        raise RuntimeError("Stopping before MQTT connection was established")

    def build_aprs_client(self, port: int) -> Any:
        """Create an APRS-IS client for a single connection attempt."""

        client = aprslib.IS(
            self.settings.aprs_callsign,
            passwd=self.settings.aprs_password,
            host=self.settings.aprs_host,
            port=port,
            skip_login=False,
        )

        if self.settings.aprs_filter:
            client.set_filter(self.settings.aprs_filter)

        return client

    def consume_aprs_forever(self) -> None:
        """Keep the APRS-IS consumer running with port failover and retry delays."""

        while not self.stop_event.is_set():
            for port in self.settings.aprs_ports_to_try:
                if self.stop_event.is_set():
                    return

                try:
                    logging.info("Connecting to APRS-IS %s:%s", self.settings.aprs_host, port)
                    client = self.build_aprs_client(port)
                    with self.aprs_client_lock:
                        self.aprs_client = client
                    client.connect(blocking=True)
                    logging.info("Connected to APRS-IS %s:%s", self.settings.aprs_host, port)
                    # consumer() blocks until the APRS stream exits or raises,
                    # so this call is the long-running APRS read loop.
                    client.consumer(self.handle_aprs_packet, blocking=True, raw=True)
                    logging.warning("APRS consumer exited cleanly. Reconnecting.")
                except (aprslib.ConnectionDrop, aprslib.ConnectionError) as exc:
                    logging.warning(
                        "APRS connection to %s:%s failed or dropped: %s",
                        self.settings.aprs_host,
                        port,
                        exc,
                    )
                except KeyboardInterrupt:
                    raise
                except Exception:
                    logging.exception(
                        "Unexpected APRS error while connected to %s:%s",
                        self.settings.aprs_host,
                        port,
                    )
                finally:
                    self.close_aprs_client()

                if self.stop_event.wait(5):
                    return

            logging.error(
                "Failed to connect to APRS-IS on ports %s. Retrying in %s seconds.",
                ", ".join(str(port) for port in self.settings.aprs_ports_to_try),
                APRS_RETRY_SECONDS,
            )

            if self.stop_event.wait(APRS_RETRY_SECONDS):
                return

    def handle_aprs_packet(self, packet: bytes | str) -> None:
        """Handle one inbound APRS packet from the APRS-IS stream."""

        if self.stop_event.is_set():
            raise StopIteration

        if not self.settings.aprs_process:
            self.publish_without_station(packet)
            return

        try:
            parsed_packet = aprslib.parse(packet)
        except (aprslib.ParseError, aprslib.UnknownFormat) as exc:
            logging.debug("Failed to parse APRS packet: %s", exc)
            self.publish_without_station(packet)
            return

        self.publish_parsed_packet(parsed_packet)

    def extract_tx_packet(self, payload_text: str) -> str:
        """Extract and validate an outbound APRS frame from a MQTT payload."""

        if not payload_text:
            raise ValueError("MQTT TX payload is empty")

        if payload_text.startswith("{") or payload_text.startswith('"'):
            try:
                decoded_payload = json.loads(payload_text)
            except json.JSONDecodeError as exc:
                raise ValueError("MQTT TX payload contains invalid JSON") from exc

            if isinstance(decoded_payload, str):
                return self.validate_outbound_packet(decoded_payload)

            if isinstance(decoded_payload, dict):
                packet_text = normalize_text(decoded_payload.get("packet"))
                if not packet_text:
                    raise ValueError("JSON MQTT TX payload must include a non-empty 'packet' field")
                return self.validate_outbound_packet(packet_text)

            raise ValueError("JSON MQTT TX payload must be a string or an object with a 'packet' field")

        return self.validate_outbound_packet(payload_text)

    def validate_outbound_packet(self, packet_text: str) -> str:
        """Apply guardrails before allowing a MQTT message onto APRS-IS."""

        packet_text = packet_text.strip()
        if not packet_text:
            raise ValueError("Outbound APRS packet is empty")
        if "\r" in packet_text or "\n" in packet_text:
            raise ValueError("Outbound APRS packet must be a single line")
        if packet_text.startswith("#"):
            raise ValueError("APRS server commands are not allowed on the MQTT TX topic")
        if ">" not in packet_text or ":" not in packet_text:
            raise ValueError("Outbound APRS packet must look like FROM>TO:BODY")

        source_callsign = packet_text.split(">", 1)[0]
        if callsign_base(source_callsign) != callsign_base(self.settings.aprs_callsign):
            raise ValueError("Outbound APRS packet source must match the configured APRS callsign base")

        return packet_text

    def send_aprs_packet(self, packet_text: str) -> None:
        """Send a single outbound APRS frame through the active APRS-IS client."""

        client_to_close = None
        send_error: Exception | None = None

        with self.aprs_client_lock:
            client = self.aprs_client
            if client is None:
                raise RuntimeError("APRS-IS is not currently connected")

            try:
                client.sendall(packet_text)
                return
            except Exception as exc:
                # Force the retry loop to establish a fresh APRS session after
                # a failed send on the cached client.
                self.aprs_client = None
                client_to_close = client
                send_error = exc

        if client_to_close is not None:
            try:
                client_to_close.close()
            except Exception:
                logging.debug("Failed to close APRS client after TX failure", exc_info=True)

        raise RuntimeError(f"APRS-IS send failed: {send_error}") from send_error

    def publish_parsed_packet(self, packet: dict[str, Any]) -> None:
        """Publish structured MQTT fields for one parsed APRS packet."""

        station_id = normalize_text(packet.get("from"))
        raw_packet = packet.get("raw")

        if not station_id:
            logging.debug("Parsed packet did not include a source callsign: %s", packet)
            if raw_packet:
                self.publish_without_station(raw_packet)
            return

        if raw_packet:
            self.publish_station_value(station_id, "raw", raw_packet)

        path = packet.get("path")
        if path:
            self.publish_station_value(station_id, "path", path)

        packet_format = packet.get("format")
        if packet_format:
            self.publish_station_value(station_id, "format", packet_format)

        symbol_table = packet.get("symbol_table")
        symbol = packet.get("symbol")
        if symbol_table and symbol:
            self.publish_station_value(station_id, "icon", f"{symbol_table}{symbol}")

        # Normalize numeric fields before publishing so MQTT payloads stay
        # stable even when aprslib returns ints, floats, or strings.
        latitude = packet.get("latitude")
        longitude = packet.get("longitude")
        if latitude is not None and longitude is not None:
            latitude_value = round(float(latitude), 4)
            longitude_value = round(float(longitude), 4)
            self.publish_station_value(station_id, "latitude", latitude_value)
            self.publish_station_value(station_id, "longitude", longitude_value)

            distance = self.get_distance(latitude_value, longitude_value)
            if distance is not None:
                self.publish_station_value(station_id, "distance", distance)

        altitude = packet.get("altitude")
        if altitude is not None:
            altitude_value = float(altitude)
            if not self.settings.metric_units:
                altitude_value /= 0.3048
            self.publish_station_value(station_id, "altitude", round(altitude_value, 0))

        speed = packet.get("speed")
        if speed is not None:
            speed_value = float(speed)
            if not self.settings.metric_units:
                speed_value *= 0.621371
            self.publish_station_value(station_id, "speed", round(speed_value, 2))

        course = packet.get("course")
        if course is not None:
            self.publish_station_value(station_id, "course", int(course))

        comment = packet.get("comment")
        if comment:
            self.publish_station_value(station_id, "comment", comment)

        telemetry = packet.get("telemetry")
        if telemetry:
            self.publish_station_value(station_id, "telemetry", telemetry)

        message_text = packet.get("message_text")
        if message_text:
            self.publish_station_value(station_id, "message", message_text)

        status = packet.get("status")
        if status:
            self.publish_station_value(station_id, "status", status)

    def publish_station_value(self, station_id: str, field_name: str, value: Any) -> None:
        """Publish one field for a specific APRS source callsign."""

        topic = f"{self.settings.mqtt_topic}/{station_id}/{field_name}"
        self.publish(topic, value)

    def publish_without_station(self, value: Any) -> None:
        """Publish a raw APRS payload when structured parsing is disabled or fails."""

        self.publish(self.settings.mqtt_topic, decode_packet(value))

    def publish_presence(self, online: bool) -> None:
        """Publish retained process liveness state to MQTT."""

        try:
            self.publish(self.settings.presence_topic, "1" if online else "0", retain=True)
        except Exception:
            logging.debug("Failed to publish presence state", exc_info=True)

    def publish_tx_status(self, ok: bool, packet_text: str, error: str | None = None) -> None:
        """Publish a structured status message for MQTT-to-APRS transmit attempts."""

        status_payload: dict[str, Any] = {
            "ok": ok,
            "packet": packet_text,
        }
        if error:
            status_payload["error"] = error

        self.publish(self.settings.mqtt_tx_status_topic, status_payload)

    def publish(self, topic: str, value: Any, *, retain: bool = False) -> None:
        """Publish a value to MQTT and log any broker-side error codes."""

        payload = mqtt_payload(value)
        logging.debug("Publishing topic %s with value %s", topic, payload)
        result = self.mqtt_client.publish(topic, payload, retain=retain)
        if result.rc != paho.MQTT_ERR_SUCCESS:
            logging.warning("Failed to publish topic %s: rc=%s", topic, result.rc)

    def get_distance(self, latitude: float, longitude: float) -> float | None:
        """Return distance from the configured reference point, if available."""

        if self.settings.aprs_latitude is None or self.settings.aprs_longitude is None:
            return None

        earth_radius_km = 6371.0088
        lat1 = radians(self.settings.aprs_latitude)
        lon1 = radians(self.settings.aprs_longitude)
        lat2 = radians(float(latitude))
        lon2 = radians(float(longitude))

        delta_lon = lon2 - lon1
        delta_lat = lat2 - lat1
        a = sin(delta_lat / 2) ** 2 + cos(lat1) * cos(lat2) * sin(delta_lon / 2) ** 2
        c = 2 * atan2(sqrt(a), sqrt(1 - a))

        distance = earth_radius_km * c
        if not self.settings.metric_units:
            distance *= 0.621371

        return round(distance, 2)

    def close_aprs_client(self) -> None:
        """Close and forget the current APRS-IS client, if one exists."""

        with self.aprs_client_lock:
            client = self.aprs_client
            self.aprs_client = None

        if client is None:
            return

        try:
            client.close()
        except Exception:
            logging.debug("Failed to close APRS client cleanly", exc_info=True)

    def request_stop(self, reason: str | None = None) -> None:
        """Initiate a clean shutdown across MQTT and APRS resources."""

        if reason:
            logging.info("Stopping mqtt-aprs: %s", reason)

        already_stopping = self.stop_event.is_set()
        self.stop_event.set()
        self.close_aprs_client()

        if not already_stopping:
            self.publish_presence(False)

        try:
            self.mqtt_client.disconnect()
        except Exception:
            logging.debug("Failed to disconnect MQTT client cleanly", exc_info=True)

        if self.mqtt_loop_started:
            self.mqtt_client.loop_stop()
            self.mqtt_loop_started = False

    def run(self) -> None:
        """Start the bridge and block until the APRS consumer exits."""

        self.connect_mqtt()
        self.consume_aprs_forever()


def install_signal_handlers(bridge: MqttAprsBridge) -> None:
    """Install SIGINT and SIGTERM handlers that request a graceful shutdown."""

    def handle_signal(signum: int, frame: Any) -> None:
        bridge.request_stop(f"signal {signum}")

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)


def build_argument_parser() -> argparse.ArgumentParser:
    """Create the command-line interface for validation and runtime startup."""

    parser = argparse.ArgumentParser(description="Bridge APRS-IS packets to MQTT topics")
    parser.add_argument(
        "-c",
        "--config",
        help="Path to mqtt-aprs.cfg. Defaults to /etc/mqtt-aprs/mqtt-aprs.cfg or a local mqtt-aprs.cfg.",
    )
    parser.add_argument(
        "--check-config",
        action="store_true",
        help="Validate configuration and exit without starting the bridge.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Program entrypoint used by both CLI execution and tests."""

    args = build_argument_parser().parse_args(argv)

    try:
        config_path = resolve_config_path(args.config)
        settings = load_settings(config_path)
    except (ConfigError, FileNotFoundError) as exc:
        print(exc, file=sys.stderr)
        return 1

    if args.check_config:
        print(f"Configuration OK: {config_path}")
        return 0

    configure_logging(settings)

    if setproctitle is not None:
        setproctitle.setproctitle(settings.app_name)

    logging.info("Starting %s", settings.app_name)
    logging.debug("Using config file %s", config_path)

    try:
        bridge = MqttAprsBridge(settings)
    except RuntimeError as exc:
        logging.error("Startup failed: %s", exc)
        return 1

    install_signal_handlers(bridge)

    try:
        bridge.run()
    except KeyboardInterrupt:
        logging.info("Interrupted by keypress")
    except RuntimeError as exc:
        logging.error("Fatal runtime error: %s", exc)
        return 1
    except Exception:
        logging.exception("Fatal unexpected error")
        return 1
    finally:
        bridge.request_stop("process exit")

    return 0


if __name__ == "__main__":
    sys.exit(main())
