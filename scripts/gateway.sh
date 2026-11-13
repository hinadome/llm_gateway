#!/usr/bin/env bash
# Start the LLM Gateway — plain HTTP, or HTTPS with a self-signed,
# private or Let's Encrypt certificate. All arguments pass through to
# `python -m gateway` (gateway/serve.py); see --help for the full list.
#
#   ./scripts/gateway.sh                                # http://127.0.0.1:8000
#   ./scripts/gateway.sh --port 8001
#   ./scripts/gateway.sh --self-signed                 # HTTPS, dev cert
#                                                       # (generated once under
#                                                       #  deploy/certs/ — keep
#                                                       #  that directory
#                                                       #  gitignored)
#   ./scripts/gateway.sh --ssl-cert my.crt --ssl-key my.key   # private CA pair
#   ./scripts/gateway.sh --letsencrypt api.example.com  # certbot's live certs
#
# Env equivalents (also read from .env via GATEWAY_* settings):
#   GATEWAY_HOST, GATEWAY_PORT, GATEWAY_SSL_CERT, GATEWAY_SSL_KEY
set -euo pipefail
cd "$(dirname "$0")/.."
exec uv run python -m gateway "$@"
