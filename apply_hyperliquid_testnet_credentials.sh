#!/usr/bin/env bash
set -euo pipefail

API_URL="${HUMMINGBOT_API_URL:-http://localhost:8000}"
API_USER="${API_USER:-admin}"
API_PASS="${API_PASS:-admin}"
ACCOUNT_NAME="${ACCOUNT_NAME:-master_account}"
CONNECTOR_NAME="${CONNECTOR_NAME:-hyperliquid_perpetual_testnet}"
CREDS_FILE="${CREDS_FILE:-.hyperliquid_perpetual_testnet_credentials.json}"

cd "$(dirname "$0")"

if [[ ! -f "$CREDS_FILE" ]]; then
  echo "Missing credentials file: $CREDS_FILE" >&2
  exit 1
fi

python3 - "$CREDS_FILE" <<'PY'
import json
import sys

path = sys.argv[1]
with open(path, "r", encoding="utf-8") as f:
    data = json.load(f)

required = [
    "hyperliquid_perpetual_testnet_mode",
    "use_vault",
    "hyperliquid_perpetual_testnet_address",
    "hyperliquid_perpetual_testnet_secret_key",
]
missing = [key for key in required if key not in data]
if missing:
    raise SystemExit(f"Missing required key(s): {', '.join(missing)}")

placeholders = [
    "0xYOUR_MASTER_ACCOUNT_ADDRESS",
    "0xYOUR_HYPERLIQUID_TESTNET_API_WALLET_PRIVATE_KEY",
]
if any(data.get(key) in placeholders for key in required):
    raise SystemExit("Credentials still contain placeholder values. Fill the JSON in TextEdit first.")

if data["hyperliquid_perpetual_testnet_mode"] not in {"api_wallet", "arb_wallet"}:
    raise SystemExit("hyperliquid_perpetual_testnet_mode must be api_wallet or arb_wallet")

if not isinstance(data["use_vault"], bool):
    raise SystemExit("use_vault must be true or false")

for key in ["hyperliquid_perpetual_testnet_address", "hyperliquid_perpetual_testnet_secret_key"]:
    value = data[key]
    if not isinstance(value, str) or not value.startswith("0x"):
        raise SystemExit(f"{key} must be a 0x-prefixed string")

print("Credentials file shape looks OK.")
PY

curl -fsS \
  -u "$API_USER:$API_PASS" \
  -H "Content-Type: application/json" \
  -X POST \
  "$API_URL/accounts/add-credential/$ACCOUNT_NAME/$CONNECTOR_NAME" \
  --data-binary "@$CREDS_FILE"

echo
echo "Credential POST completed. Current credentials for $ACCOUNT_NAME:"
curl -fsS -u "$API_USER:$API_PASS" "$API_URL/accounts/$ACCOUNT_NAME/credentials"
echo
