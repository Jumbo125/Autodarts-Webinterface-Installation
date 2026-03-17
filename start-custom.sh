#!/usr/bin/env bash
set -e

cd /var/lib/autodarts/extensions/darts-wled
source .venv/bin/activate

# Player/Idle (bleibt stehen)
# Events (mit Sekunden)

exec python darts-wled.py \
  -CON "127.0.0.1:8079" \
  -WEPS "Dart-Led1.local" \
  -IDE  "ps|1" \
  -IDE2 "ps|2" \
  -IDE3 "ps|3" \
  -IDE4 "ps|4" \
  -IDE5 "ps|5" \
  -IDE6 "ps|6" \
  -A1   0-0  "ps|7|3" \
  -S0          "ps|8|3" \
  -TOE         "ps|9|3" \
  -DSBULL      "ps|10|3" \
  -S180        "ps|11|3" \
  -G           "ps|12|4" \
  -M           "ps|13|4"
