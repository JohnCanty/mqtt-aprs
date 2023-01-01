# mqtt-aprs
Connects to the specified APRS-IS server, and posts the APRS output to MQTT.  Can parse parameters, or dump the raw JSON from aprslib.  It's currently for receive only from APRS-IS and sending to an MQTT server.

This script uses the aprslib, https://github.com/rossengeorgiev/aprs-python, to do the heavy APRS lifting.

This is a fork from https://github.com/eloebl/mqtt-aprs

Making some updates to run on Bullseye

INSTALL
=================
```
sudo apt-get install git python-pip

sudo pip install setuptools
sudo pip install setproctitle
sudo pip install paho-mqtt
sudo pip install aprslib
sudo pip install configparser

sudo mkdir /etc/mqtt-aprs/
sudo mkdir 
sudo git clone git://github.com/JohnCanty/mqtt-aprs.git /usr/local/mqtt-aprs/

If write permissions or cloning seem to not work:
cd ~
mkdir mqtt-aprs
cd mqtt-aprs
git clone git://github.com/JohnCanty/mqtt-aprs.git
cp * /etc/mqtt-aprs/

sudo cp /usr/local/mqtt-aprs/mqtt-aprs.cfg.example /etc/mqtt-aprs/mqtt-aprs.cfg
Modify this with your information. Add your callsign, your password, and any other filters you are looking to apply.

sudo cp /usr/local/mqtt-aprs/mqtt-aprs.default /etc/default/mqtt-aprs
sudo cp /usr/local/mqtt-aprs/mqtt-aprs.service /lib/systemd/system/
```
Load the Systemd service file:
`sudo systemctl daemon-reload`

Enable the service:
`sudo systemctl enable mqtt-aprs`

Start the service:
`sudo systemctl start mqtt-aprs`

APRS is a registered trademark Bob Bruninga, WB4APR

Forked from original https://github.com/kylegordon/mqtt-owfs-temp, and customised for use with APRS
