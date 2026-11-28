#!/bin/sh
set -eu
cd "$(dirname "$0")/.."
test -x .venv/bin/python || { echo 'Create .venv and install requirements first' >&2; exit 1; }
test -f .env || { echo 'Create .env first' >&2; exit 1; }
ROOT=$(pwd)
SENSOR_TOKEN=$(sed -n 's/^SENSOR_TOKEN=//p' .env)
SENSOR_PORT=$(sed -n 's/^SENSOR_PORT=//p' .env)
test ${#SENSOR_TOKEN} -ge 24 || { echo 'SENSOR_TOKEN must have at least 24 characters' >&2; exit 1; }
case "$SENSOR_PORT" in ''|*[!0-9]*) echo 'SENSOR_PORT must be numeric' >&2; exit 1;; esac
xml_escape() { printf '%s' "$1" | sed -e 's/&/\&amp;/g' -e 's/</\&lt;/g' -e 's/>/\&gt;/g'; }
OPTIONAL_ENV=''
for NAME in OLLAMA_URL TOKEN_TELEMETRY_PATH; do
  VALUE=$(sed -n "s/^$NAME=//p" .env)
  if [ -n "$VALUE" ]; then
    OPTIONAL_ENV="$OPTIONAL_ENV<key>$NAME</key><string>$(xml_escape "$VALUE")</string>"
  fi
done
mkdir -p audits
PLIST="$ROOT/.sensor.launchd.plist"
cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>Label</key><string>com.telemetry-observatory.sensor</string>
<key>ProgramArguments</key><array><string>$ROOT/.venv/bin/python</string><string>$ROOT/host_sensor.py</string></array>
<key>WorkingDirectory</key><string>$ROOT</string>
<key>EnvironmentVariables</key><dict><key>SENSOR_TOKEN</key><string>$SENSOR_TOKEN</string><key>SENSOR_PORT</key><string>$SENSOR_PORT</string>$OPTIONAL_ENV</dict>
<key>RunAtLoad</key><true/><key>KeepAlive</key><true/>
<key>StandardOutPath</key><string>$ROOT/audits/sensor-console.log</string>
<key>StandardErrorPath</key><string>$ROOT/audits/sensor-console.log</string>
</dict></plist>
EOF
chmod 600 "$PLIST"
launchctl bootout "gui/$(id -u)/com.telemetry-observatory.sensor" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"
echo 'Host sensor installed and started'
