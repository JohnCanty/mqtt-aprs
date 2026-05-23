# mqtt-aprs

mqtt-aprs bridges APRS-IS traffic into MQTT topics and can optionally send APRS packets back to APRS-IS from a dedicated MQTT command topic.

The program is designed for unattended service use:

- It validates configuration before startup.
- It reconnects to MQTT and APRS-IS with retry loops.
- It can publish either structured APRS fields or raw packets.
- It publishes a retained MQTT presence topic for service monitoring.
- It supports an explicit, opt-in MQTT-to-APRS transmit path.

This project uses [aprslib](https://github.com/rossengeorgiev/aprs-python) for APRS-IS connectivity and APRS parsing.

It is a descendant of the original mqtt-owfs-temp work by Kyle Gordon and the mqtt-aprs fork by eloebl.

## Documentation Map

- This README is the operator guide for installation, configuration, startup, and troubleshooting.
- [docs/PROGRAM_REFERENCE.md](docs/PROGRAM_REFERENCE.md) is the full runtime and code-structure reference.
- [mqtt-aprs.cfg.example](mqtt-aprs.cfg.example) is the annotated sample configuration.
- [mqtt-aprs.service](mqtt-aprs.service) is the bundled systemd unit for unattended service deployment.
- [mqtt-aprs.default](mqtt-aprs.default) is a legacy defaults file retained in the repository for compatibility history; the bundled systemd unit does not currently consume it.

## Features

- Receive APRS-IS packets and publish them to MQTT.
- Publish structured fields such as position, altitude, distance, icon, telemetry, and message text.
- Fall back to raw packet publishing when parsing is disabled or a packet is unsupported.
- Retry APRS-IS connections across common APRS ports.
- Expose a config validation mode with `--check-config`.
- Optionally transmit APRS packets from MQTT through APRS-IS.

## Data Flow

1. The service connects to the MQTT broker.
2. It publishes `RF/<MQTT_SUBTOPIC>/state` with `1` while online and `0` when stopping.
3. It connects to APRS-IS and consumes packets.
4. If `APRS_PROCESS = True`, parsed fields are published under per-station MQTT topics.
5. If `APRS_PROCESS = False`, only the raw packet is published to the base MQTT topic.
6. If `MQTT_TX_ENABLE = True`, the service subscribes to a dedicated MQTT topic and forwards approved APRS frames to APRS-IS.

For the full startup sequence, retry model, topic serialization rules, and class/function map, see [docs/PROGRAM_REFERENCE.md](docs/PROGRAM_REFERENCE.md).

## MQTT Topic Layout

### Receive topics

Base topic:

`RF/<MQTT_SUBTOPIC>`

Presence topic:

`RF/<MQTT_SUBTOPIC>/state`

Structured per-station topics when `APRS_PROCESS = True`:

- `RF/<MQTT_SUBTOPIC>/<CALLSIGN>/raw`
- `RF/<MQTT_SUBTOPIC>/<CALLSIGN>/path`
- `RF/<MQTT_SUBTOPIC>/<CALLSIGN>/format`
- `RF/<MQTT_SUBTOPIC>/<CALLSIGN>/icon`
- `RF/<MQTT_SUBTOPIC>/<CALLSIGN>/latitude`
- `RF/<MQTT_SUBTOPIC>/<CALLSIGN>/longitude`
- `RF/<MQTT_SUBTOPIC>/<CALLSIGN>/distance`
- `RF/<MQTT_SUBTOPIC>/<CALLSIGN>/altitude`
- `RF/<MQTT_SUBTOPIC>/<CALLSIGN>/speed`
- `RF/<MQTT_SUBTOPIC>/<CALLSIGN>/course`
- `RF/<MQTT_SUBTOPIC>/<CALLSIGN>/comment`
- `RF/<MQTT_SUBTOPIC>/<CALLSIGN>/telemetry`
- `RF/<MQTT_SUBTOPIC>/<CALLSIGN>/message`
- `RF/<MQTT_SUBTOPIC>/<CALLSIGN>/status`

### Transmit topics

When `MQTT_TX_ENABLE = True`, the service subscribes to:

`MQTT_TX_TOPIC`

If `MQTT_TX_TOPIC` is left blank, it defaults to:

`RF/<MQTT_SUBTOPIC>/tx`

Transmit result messages are published to:

`<MQTT_TX_TOPIC>/status`

Status payloads are JSON objects with these fields:

- `ok`: Boolean success flag.
- `packet`: The APRS packet text that was accepted or rejected.
- `error`: Present only when transmission failed or was rejected.

## MQTT-to-APRS Transmission

Transmission is disabled by default and should be enabled only when you intend to send packets to APRS-IS.

Safety rules enforced by the program:

- `MQTT_TX_ENABLE` must be `True`.
- `APRS_PASSWORD` must be a verified APRS passcode and cannot be `-1`.
- The payload must be a single APRS frame on one line.
- The outbound frame source callsign must match the configured APRS callsign base.
- APRS-IS server commands such as lines beginning with `#` are rejected.

Accepted MQTT TX payload formats:

Raw text payload:

```text
N0CALL>APRS,TCPIP*:>status text
```

JSON payload:

```json
{"packet": "N0CALL>APRS,TCPIP*:>status text"}
```

Example publishes:

```bash
mosquitto_pub -h 10.0.0.1 -t RF/aprs/tx -m 'N0CALL>APRS,TCPIP*:>status text'
mosquitto_pub -h 10.0.0.1 -t RF/aprs/tx -m '{"packet":"N0CALL>APRS,TCPIP*:>status text"}'
```

Worked transmit example:

```text
N0CALL-10>APRS,TCPIP*:>mqtt-aprs test packet
```

Packet breakdown:

- `N0CALL-10` is the source callsign/SSID. Its base callsign must match `APRS_CALLSIGN`.
- `APRS,TCPIP*` is the destination and APRS-IS path.
- `:>mqtt-aprs test packet` is the APRS information field, in this case a status payload.

Equivalent test publish:

```bash
mosquitto_pub -h 10.0.0.1 -t RF/aprs/tx -m 'N0CALL-10>APRS,TCPIP*:>mqtt-aprs test packet'
```

## Configuration Reference

All settings live in the `[global]` section.

### MQTT settings

| Option | Required | Description |
| --- | --- | --- |
| `DEBUG` | No | Enables debug logging when `True`. |
| `LOGFILE` | No | Path to an additional runtime log file. Logs always go to stderr/journal; when this is set they are also written to the file. |
| `MQTT_HOST` | Yes | MQTT broker hostname or IP address. |
| `MQTT_PORT` | No | MQTT broker TCP port. Defaults to `1883`. |
| `MQTT_SUBTOPIC` | Yes | Topic suffix published under `RF/<MQTT_SUBTOPIC>`. |
| `MQTT_USERNAME` | No | MQTT username. |
| `MQTT_PASSWORD` | No | MQTT password. |
| `MQTT_TX_ENABLE` | No | Enables MQTT-to-APRS transmission when `True`. Defaults to `False`. |
| `MQTT_TX_TOPIC` | No | Command topic for outbound APRS frames. Defaults to `RF/<MQTT_SUBTOPIC>/tx`. |

### APRS settings

| Option | Required | Description |
| --- | --- | --- |
| `APRS_CALLSIGN` | Yes | APRS-IS login callsign. |
| `APRS_PASSWORD` | Yes | APRS passcode. Use `-1` for receive-only mode. A valid passcode is required when `MQTT_TX_ENABLE` is `True`. |
| `APRS_HOST` | Yes | APRS-IS hostname. |
| `APRS_PORT` | No | Preferred APRS-IS port. Defaults to `14580`. |
| `APRS_FILTER` | No | APRS-IS server-side filter string. |
| `APRS_PROCESS` | No | When `True`, publish parsed fields. When `False`, publish only raw packets. |
| `APRS_LATITUDE` | No | Reference latitude used to calculate distance. Must be set together with `APRS_LONGITUDE`. |
| `APRS_LONGITUDE` | No | Reference longitude used to calculate distance. Must be set together with `APRS_LATITUDE`. |
| `METRICUNITS` | No | When `True`, publish metric values. When `False`, convert distance, speed, and altitude to imperial units. |

## Installation

```bash
apt update
apt install -y git python3 python3-venv python3-pip ca-certificates nano

/usr/sbin/useradd --system --user-group --no-create-home --shell /usr/sbin/nologin mqtt-aprs

git clone https://github.com/JohnCanty/mqtt-aprs /opt/mqtt-aprs
chown -R mqtt-aprs:mqtt-aprs /opt/mqtt-aprs

python3 -m venv /opt/mqtt-aprs/venv
/opt/mqtt-aprs/venv/bin/pip install --upgrade pip setuptools
/opt/mqtt-aprs/venv/bin/pip install setproctitle paho-mqtt aprslib

mkdir -p /etc/mqtt-aprs
cp /opt/mqtt-aprs/mqtt-aprs.cfg.example /etc/mqtt-aprs/mqtt-aprs.cfg
chown -R mqtt-aprs:mqtt-aprs /etc/mqtt-aprs

touch /var/log/mqtt-aprs.log
chown mqtt-aprs:mqtt-aprs /var/log/mqtt-aprs.log
chmod 640 /var/log/mqtt-aprs.log

cp /opt/mqtt-aprs/mqtt-aprs.service /etc/systemd/system/mqtt-aprs.service
```

## Validation And Startup

Edit the configuration:

```bash
nano /etc/mqtt-aprs/mqtt-aprs.cfg
```

Validate it before starting the service:

```bash
/opt/mqtt-aprs/venv/bin/python /opt/mqtt-aprs/mqtt-aprs.py --check-config
```

Load and start the systemd service:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now mqtt-aprs
```

Runtime notes:

- The default config path is `/etc/mqtt-aprs/mqtt-aprs.cfg`.
- You can override the config path with `--config /path/to/mqtt-aprs.cfg`.
- You can also override it with the `MQTT_APRS_CONFIG` environment variable.
- The bundled systemd unit runs `--check-config` before starting the long-running service process.
- The bundled systemd unit starts after `network.target`. The bridge itself retries MQTT and APRS connection failures, so it does not rely on `network-online.target`.

## Operational Checks

Show service status:

```bash
sudo systemctl status mqtt-aprs
```

Run the service manually in the foreground:

```bash
/opt/mqtt-aprs/venv/bin/python /opt/mqtt-aprs/mqtt-aprs.py --config /etc/mqtt-aprs/mqtt-aprs.cfg
```

Show recent service logs:

```bash
sudo journalctl -u mqtt-aprs -n 100 --no-pager
```

Show logs from the current boot with monotonic timestamps:

```bash
sudo journalctl -b -u mqtt-aprs -o short-monotonic --no-pager
```

If `LOGFILE` is set, the same runtime messages are also written to that file.

Watch MQTT output live during testing:

```bash
mosquitto_sub -h 10.0.0.1 -v -t 'RF/aprs/#'
```

## Troubleshooting

- If the service starts with the wrong `ExecStart`, copy the bundled service file again and run `sudo systemctl daemon-reload`.
- If the service only misbehaves during boot, inspect `sudo journalctl -b -u mqtt-aprs -o short-monotonic --no-pager` first. The bundled unit validates config before launch, and the process itself keeps retrying until MQTT and APRS become reachable.
- If `--check-config` fails, fix the missing or invalid settings before starting the service.
- If the transmit topic rejects packets, confirm the frame is single-line and starts with the configured callsign base.
- If APRS transmission is enabled but packets are rejected by APRS-IS, verify that `APRS_PASSWORD` is a valid passcode for `APRS_CALLSIGN`.

For detailed internals, field-by-field MQTT payload rules, and lifecycle behavior, see [docs/PROGRAM_REFERENCE.md](docs/PROGRAM_REFERENCE.md).

## Improvement Areas

The current codebase is serviceable, but there are still worthwhile next steps:

- Add automated tests for config parsing, packet validation, and MQTT topic mapping.
- Publish richer typed payloads for list-like fields such as APRS paths if downstream consumers prefer JSON everywhere.
- Expose optional metrics or health endpoints for long-running service monitoring.

APRS is a registered trademark of Bob Bruninga, WB4APR.
