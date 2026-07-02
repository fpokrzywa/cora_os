#!/usr/bin/env bash
# Deploy the patched voice pipeline to the DGX (spark-a84c) and restart it.
#
# The patch is INERT until the env flags from install-voice-token.sh are set:
# with the old env, behavior is byte-for-byte the pre-patch pipeline.
# Backout: restore ~/cora/phase1_push_to_talk.py.bak-pre-cora-api (created
# here) and the env backup from install-voice-token.sh, then restart.
set -euo pipefail

DGX="fpokrzywa@spark-a84c"
SSH=(ssh -o IdentitiesOnly=yes -o StrictHostKeyChecking=no -i /home/owner/.ssh/id_dgx_spark)
SRC="$(cd "$(dirname "$0")" && pwd)/phase1_push_to_talk.py"

echo "[1/4] backing up the current pipeline on the DGX"
"${SSH[@]}" "$DGX" 'cp -n ~/cora/phase1_push_to_talk.py ~/cora/phase1_push_to_talk.py.bak-pre-cora-api && ls -la ~/cora/phase1_push_to_talk.py.bak-pre-cora-api'

echo "[2/4] copying the patched pipeline"
scp -o IdentitiesOnly=yes -o StrictHostKeyChecking=no -i /home/owner/.ssh/id_dgx_spark "$SRC" "$DGX":cora/phase1_push_to_talk.py

echo "[3/4] compile check on the DGX venv"
"${SSH[@]}" "$DGX" '~/cora/venv/bin/python -m py_compile ~/cora/phase1_push_to_talk.py && echo compile-ok'

echo "[4/4] restarting cora-voice"
if "${SSH[@]}" "$DGX" 'sudo -n systemctl restart cora-voice 2>/dev/null'; then
  echo "restarted via sudo"
else
  echo "passwordless sudo unavailable — run this on the DGX yourself:"
  echo "    sudo systemctl restart cora-voice"
  exit 0
fi
sleep 3
"${SSH[@]}" "$DGX" 'systemctl is-active cora-voice'
echo "done — check the [config]/[llm] banner in the service log for the new base_url"
