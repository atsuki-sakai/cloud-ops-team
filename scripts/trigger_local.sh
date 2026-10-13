#!/usr/bin/env bash
# ローカルの ADK サーバー（agents-cli playground / uv run uvicorn app.fast_api_app:app）に
# Pub/Sub push と同じ形式でジョブを投げる。
#   ./scripts/trigger_local.sh error_watch '"hours":2'
#   ./scripts/trigger_local.sh weekly_pr_review
#   ./scripts/trigger_local.sh monthly_security_audit '"kev_days":30'
set -euo pipefail
JOB="${1:-error_watch}"
EXTRA="${2:-}"
BASE_URL="${AGENT_URL:-http://localhost:8000}"
APP_NAME="${APP_NAME:-app}"
PAYLOAD="{\"job\":\"${JOB}\"${EXTRA:+,${EXTRA}}}"
DATA=$(printf '%s' "$PAYLOAD" | base64 | tr -d '\n')
echo "→ POST ${BASE_URL}/apps/${APP_NAME}/trigger/pubsub  data=${PAYLOAD}"
curl -sS -X POST "${BASE_URL}/apps/${APP_NAME}/trigger/pubsub" \
  -H 'content-type: application/json' \
  -d "{\"message\":{\"data\":\"${DATA}\",\"messageId\":\"local-$(date +%s)\"},\"subscription\":\"projects/local/subscriptions/manual\"}"
echo
