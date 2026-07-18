#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
DEMO_DIR="${ROOT}/third_party/opentelemetry-demo"
BRIDGE="${ROOT}/integrations/astronomy-shop/compose.aiops-bridge.yaml"
if [[ ! -f "${DEMO_DIR}/compose.yaml" ]]; then
  echo "Demo not cloned; nothing to stop."
  exit 0
fi
cd "$DEMO_DIR"
docker compose --env-file .env --env-file .env.override \
  -f compose.yaml -f compose.extras.yaml \
  -f "$BRIDGE" \
  down --remove-orphans
echo "Astronomy Shop stopped. AIOps stack left running."
