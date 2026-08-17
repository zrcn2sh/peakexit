#!/usr/bin/env bash
# PeakExit → 미니PC (rsync, .git 제외) — WSL / macOS / Linux
# 사용: ./scripts/deploy-to-minipc.sh user@192.168.0.137 ~/peakexit

set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

REMOTE="${1:?usage: $0 user@host [/remote/path]}"
REMOTE_DIR="${2:-~/peakexit}"

RSYNC_SSH="${RSYNC_RSH:-ssh}"
export RSYNC_RSH

echo ">> rsync ( .git 제외 ) -> ${REMOTE}:${REMOTE_DIR}/"
rsync -avz --delete \
  --exclude '.git/' \
  --exclude '.env' \
  --exclude '.env.*' \
  --exclude '__pycache__/' \
  --exclude '*.pyc' \
  --exclude '.venv/' \
  --exclude 'venv/' \
  --exclude 'data/' \
  --exclude 'backend/data/' \
  --exclude 'node_modules/' \
  --exclude '.idea/' \
  --exclude '.vscode/' \
  --exclude 'agent-tools/' \
  --exclude 'agent-transcripts/' \
  --exclude 'mcps/' \
  --exclude 'terminals/' \
  -e "$RSYNC_SSH" \
  ./ "${REMOTE}:${REMOTE_DIR}/"

echo ">> docker compose 재빌드"
ssh "$REMOTE" "cd '${REMOTE_DIR}' && docker compose build && docker compose up -d"
echo ">> 완료"
