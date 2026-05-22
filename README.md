# mqtt-aprs
Connects to the specified APRS-IS server, and posts the APRS output to MQTT.  Can parse parameters, or dump the raw JSON from aprslib.  It's currently for receive only from APRS-IS and sending to an MQTT server.

This script uses the aprslib, https://github.com/rossengeorgiev/aprs-python, to do the heavy APRS lifting.

This is a fork from https://github.com/eloebl/mqtt-aprs

Making some updates to run on Bullseye

INSTALL
=================
```
Install Puthon and dependencies:

apt update
apt install -y git python3 python3-venv python3-pip ca-certificates nano


Create the User:
- If logged in as root, you may have to use the full path here

/usr/sbin/useradd --system --user-group --no-create-home --shell /usr/sbin/nologin mqtt-aprs


Download this repo:

git clone https://github.com/JohnCanty/mqtt-aprs /opt/mqtt-aprs
chown -R mqtt-aprs:mqtt-aprs /opt/mqtt-aprs

Create the Virtual environment also adding dependencies:

python3 -m venv /opt/mqtt-aprs/venv
/opt/mqtt-aprs/venv/bin/pip install --upgrade pip setuptools
/opt/mqtt-aprs/venv/bin/pip install setproctitle paho-mqtt aprslib


Move the config file to an expected location:

mkdir -p /etc/mqtt-aprs
cp /opt/mqtt-aprs/mqtt-aprs.cfg.example /etc/mqtt-aprs/mqtt-aprs.cfg
chown -R mqtt-aprs:mqtt-aprs /etc/mqtt-aprs


Edit your config:
- You may find that you want more control of where things are published - some code edits should be done only after initial startup.

nano /etc/mqtt-aprs/mqtt-aprs.cfg

Validate the config before starting the service:

/opt/mqtt-aprs/venv/bin/python /opt/mqtt-aprs/mqtt-aprs.py --check-config

Setup the logs:

touch /var/log/mqtt-aprs.log
chown mqtt-aprs:mqtt-aprs /var/log/mqtt-aprs.log
chmod 640 /var/log/mqtt-aprs.log


Install the service file:

cp /opt/mqtt-aprs/mqtt-aprs.service /etc/systemd/system/mqtt-aprs.service
```
Load the Systemd service file:
`sudo systemctl daemon-reload`
`systemctl enable --now mqtt-aprs`

Runtime notes:
- The script looks for `/etc/mqtt-aprs/mqtt-aprs.cfg` by default.
- You can override the config path with `--config /path/to/mqtt-aprs.cfg` or the `MQTT_APRS_CONFIG` environment variable.
- The MQTT publish base is `RF/<MQTT_SUBTOPIC>`.


APRS is a registered trademark Bob Bruninga, WB4APR

Forked from original https://github.com/kylegordon/mqtt-owfs-temp, and customised for use with APRS
