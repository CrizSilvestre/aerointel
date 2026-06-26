#!/bin/bash
# run-loop.sh — corre el pipeline en bucle para que el dashboard se actualice solo.
# Funciona mientras esté encendido y la Mac despierta. Ctrl+C para parar.
#   bash run-loop.sh
# Opcional: AEROINTEL_INTERVAL=900 bash run-loop.sh   (intervalo en segundos; 1800 = 30 min)
#           ANTHROPIC_API_KEY / MATTERMOST_WEBHOOK_URL para LLM real y publicar.
cd "$(dirname "$0")" || exit 1
INTERVAL="${AEROINTEL_INTERVAL:-1800}"
echo "AeroIntel · auto-update cada ${INTERVAL}s ($((INTERVAL/60)) min) · Ctrl+C para parar"
while true; do
  python3 aerointel.py
  echo "──── próxima actualización en $((INTERVAL/60)) min · $(date '+%Y-%m-%d %H:%M') ────"
  sleep "$INTERVAL"
done
