#!/usr/bin/env bash
# Mint a 365-day cora-api JWT for the voice service and install the
# brain-swap env on the DGX. Run from the orchestration host (needs the
# cora-api container running + DGX SSH). The token is NEVER printed.
#
# What it writes to ~/.config/cora/env on spark-a84c (after a timestamped
# backup): CORA_LLM_* pointed at cora-api's OpenAI façade + the cleanup
# pass pinned to the DGX-local Ollama.
#
# Backout: restore the printed env backup file and restart cora-voice —
# the voice reverts to the old local-Qwen brain.
set -euo pipefail

DGX="fpokrzywa@spark-a84c"
SSH=(ssh -o IdentitiesOnly=yes -o StrictHostKeyChecking=no -i /home/owner/.ssh/id_dgx_spark)
API_BASE="https://cora.tail343b33.ts.net:8443"

MINT=$(mktemp)
trap 'rm -f "$MINT"' EXIT
cat > "$MINT" <<'PYEOF'
import asyncio, os, sys
from datetime import datetime, timedelta, timezone
import asyncpg, jwt
from app.config import settings

async def main():
    db = await asyncpg.connect(os.environ["DATABASE_URL"])
    row = await db.fetchrow(
        "SELECT id, email, role FROM users WHERE LOWER(email)=LOWER($1)",
        "freddie@3cpublish.com",
    )
    if row is None:
        row = await db.fetchrow(
            "SELECT id, email, role FROM users ORDER BY created_at LIMIT 1"
        )
    await db.close()
    now = datetime.now(timezone.utc)
    payload = {
        "sub": str(row["id"]), "email": row["email"], "role": row["role"],
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(days=365)).timestamp()),
    }
    print(f"minting 365d voice token for {row['email']}", file=sys.stderr)
    print(jwt.encode(payload, settings.jwt_secret, algorithm="HS256"))

asyncio.run(main())
PYEOF

docker cp "$MINT" cora-api:/tmp/mint_voice_token.py
TOKEN=$(docker exec -e PYTHONPATH=/app cora-api python /tmp/mint_voice_token.py)
docker exec cora-api rm -f /tmp/mint_voice_token.py
[ -n "$TOKEN" ] || { echo "token mint failed" >&2; exit 1; }
echo "token captured (${#TOKEN} chars) — validating against /auth/me"
docker exec cora-api python - "$TOKEN" <<'PYEOF'
import json, sys, urllib.request
req = urllib.request.Request("http://127.0.0.1:8000/auth/me")
req.add_header("Authorization", "Bearer " + sys.argv[1])
print("token authenticates as:", json.load(urllib.request.urlopen(req))["email"])
PYEOF

echo "installing env on the DGX (with backup)"
"${SSH[@]}" "$DGX" "cp ~/.config/cora/env ~/.config/cora/env.bak-\$(date +%Y%m%d-%H%M%S) && ls ~/.config/cora/env.bak-* | tail -1 && sed -i '/^CORA_LLM_/d;/^CORA_CLEANUP_BASE_URL/d;/^CORA_CLEANUP_MODEL/d' ~/.config/cora/env && cat >> ~/.config/cora/env && chmod 600 ~/.config/cora/env" <<EOF2
CORA_LLM_PROVIDER=openai
CORA_LLM_BASE_URL=${API_BASE}/v1
CORA_LLM_MODEL=cora-api
CORA_LLM_API_KEY=${TOKEN}
CORA_LLM_SESSION_HEADERS=1
CORA_CLEANUP_BASE_URL=http://localhost:11434
CORA_CLEANUP_MODEL=cora-qwen3:4b
EOF2
echo "env installed. Now restart the service (deploy.sh does this, or:"
echo "    ssh $DGX 'sudo systemctl restart cora-voice'"
echo ")"
