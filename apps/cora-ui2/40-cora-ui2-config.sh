#!/bin/sh
# Runs from the official nginx image's /docker-entrypoint.d hook: bake the
# runtime cora-api base URL into config.js (no build step in this UI).
set -e
API_URL="${CORA_API_URL:-http://api.cora.local.arpa}"
cat > /usr/share/nginx/html/config.js <<EOF
// Generated at container start from CORA_API_URL.
window.CORA_UI2_CONFIG = { apiBase: "${API_URL}" };
EOF
