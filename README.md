# Bambu Cloud progress reader

Read-only Python service for a Bambu Lab printer connected through Bambu Cloud.
It publishes print progress through MQTT Discovery to Home Assistant.

#Motivation
The Bambu Lab integration for Home Assistant is excellent and feature-rich. For this project, however, I only needed a small, focused set of print-status data: the current layer, total number of layers, and print progress.

Rather than install the full HACS integration and add a large number of  entities I don't need to Home Assistant and its database, this lightweight service retrieves only the required data from Bambu Cloud and publishes it through MQTT Discovery.
This service requires a locally available MQTT broker that Home Assistant can access. It also needs Bambu Cloud credentials: an API access token, the account user ID, and the printer device ID (serial number).

```json
{"device_id":"01S00A...","state":"RUNNING","current_layer":125,"total_layers":250,"progress_percent":45,"remaining_minutes":360}
```

## Run locally

```bash
cd bambu-cloud-progress
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
set -a; source .env; set +a
python bambu_progress.py
```

Create `.env` by copying `.env.example` and fill in the three required values.
Do not commit `.env`: the access token grants access to the Bambu Cloud account.

`BAMBU_DEVICE_ID` is the printer's serial number. `BAMBU_USER_ID` is the
numeric `uid` (the script itself adds the MQTT `u_` prefix).

The program subscribes to `device/<device id>/report` and sends only the
`pushing.pushall` status request. It does not send print-control commands.

## Container

Build the image:

```bash
docker build -t bambu-progress .
```

Run it once with values passed through an environment file:

```bash
docker run --rm --env-file bambu-progress.env bambu-progress:latest
```

It publishes MQTT Discovery configuration and one retained JSON state record to
your local MQTT broker, then exits. Home Assistant automatically creates one
device and sensors for current layer, total layers, progress, remaining time,
print state and last successful update.

## Docker Compose service

Copy and edit the supplied environment file:

```bash
cp bambu-progress.env.example bambu-progress.env
chmod 600 bambu-progress.env
nano bambu-progress.env
```

Fill in the Bambu Cloud credentials and the local MQTT broker credentials. Set:

```text
REFRESH_SECONDS=60
```

This is the only scheduling setting. The container stays up, fetches a new
report every 60 seconds and retries on the next interval after a transient
error. `REFRESH_SECONDS=0` retains the one-shot behavior for manual testing.

Start the service:

```bash
docker compose up --build -d
docker compose logs -f bambu-progress
```

Stop it:

```bash
docker compose down
```

The `compose.yaml` uses `env_file: bambu-progress.env` and
`restart: unless-stopped`. Keep this secret file outside Git; it is also
excluded from the Docker build context.

Home Assistant automatically creates one **Bambu Lab A1** device and these
retained entities after the first successful poll:

- Aktualna warstwa
- Wszystkie warstwy
- Postęp druku
- Pozostały czas
- Stan druku
- Ostatnia aktualizacja

The service publishes an MQTT availability state for the collector itself. If
the container stops, the entities become unavailable. If only Bambu Cloud fails,
the collector remains online and `Ostatnia aktualizacja` stops changing.

Environment variables:

```text
BAMBU_ACCESS_TOKEN=...
BAMBU_USER_ID=12345678
BAMBU_DEVICE_ID=01S00A123456789
HA_MQTT_HOST=192.168.1.10
HA_MQTT_PORT=1883
HA_MQTT_USERNAME=...
HA_MQTT_PASSWORD=...
REFRESH_SECONDS=60
```
TODO
1. custom printer name (version mess to fix)
2. localization
3. last update entity

