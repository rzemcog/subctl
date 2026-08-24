#!/usr/bin/env bash
set -Eeuo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON_BIN="${PYTHON:-python3}"
TMP_DIR="$(mktemp -d)"
PIDS=()

cleanup() {
  local status=$?
  trap - EXIT INT TERM
  for pid in "${PIDS[@]:-}"; do
    if kill -0 "$pid" 2>/dev/null; then
      kill "$pid" 2>/dev/null || true
    fi
  done
  for pid in "${PIDS[@]:-}"; do
    wait "$pid" 2>/dev/null || true
  done
  rm -rf "$TMP_DIR"
  exit "$status"
}
trap cleanup EXIT INT TERM

require_command() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "missing required command: $1" >&2
    exit 127
  fi
}

wait_for_file() {
  local path="$1"
  local attempts=100
  while ((attempts > 0)); do
    if [[ -s "$path" ]]; then
      return 0
    fi
    sleep 0.05
    attempts=$((attempts - 1))
  done
  echo "timed out waiting for $path" >&2
  exit 1
}

start_server() {
  local mode="$1"
  local root="$2"
  local port_file="$TMP_DIR/${mode}.port"
  local log_file="$TMP_DIR/${mode}.log"

  "$PYTHON_BIN" "$TMP_DIR/server.py" "$mode" "$root" "$port_file" >"$log_file" 2>&1 &
  PIDS+=("$!")
  wait_for_file "$port_file"
  cat "$port_file"
}

fetch_url() {
  local url="$1"
  local output="$2"
  "$PYTHON_BIN" - "$url" "$output" <<'PY'
import sys
import urllib.request

url, output = sys.argv[1:3]
request = urllib.request.Request(url, headers={"User-Agent": "subctl-smoke/1"})
with urllib.request.urlopen(request, timeout=5) as response:
    status = getattr(response, "status", 200)
    if status >= 400:
        raise SystemExit(f"HTTP {status} for {url}")
    body = response.read()
with open(output, "wb") as handle:
    handle.write(body)
PY
}

require_command "$PYTHON_BIN"

cat >"$TMP_DIR/server.py" <<'PY'
from __future__ import annotations

import functools
import sys
from http.server import BaseHTTPRequestHandler, SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


class QuietHandler(BaseHTTPRequestHandler):
    body = b""
    expected_path = "/"

    def do_GET(self):
        if self.path != self.expected_path:
            self.send_response(404)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(self.body)

    def log_message(self, format, *args):
        return


class ProviderHandler(QuietHandler):
    expected_path = "/subscription"
    body = b"trojan://pass@provider.mock:443#provider-node\n"


class XuiHandler(QuietHandler):
    expected_path = "/sub/alice"
    body = b"ss://YWVzLTEyOC1nY206cGFzcw@private.mock:443#alice-node\n"


class StaticHandler(SimpleHTTPRequestHandler):
    def log_message(self, format, *args):
        return


mode, root, port_file = sys.argv[1:4]
if mode == "provider":
    handler = ProviderHandler
elif mode == "xui":
    handler = XuiHandler
elif mode == "static":
    handler = functools.partial(StaticHandler, directory=root)
else:
    raise SystemExit(f"unknown server mode: {mode}")

server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
Path(port_file).write_text(str(server.server_address[1]), encoding="utf-8")
server.serve_forever()
PY

STATE_DIR="$TMP_DIR/state"
PUBLIC_DIR="$TMP_DIR/public"
CONFIG_PATH="$TMP_DIR/config.yaml"
USERS_PATH="$TMP_DIR/users.yaml"
mkdir -p "$STATE_DIR" "$PUBLIC_DIR"

PROVIDER_PORT="$(start_server provider "$TMP_DIR")"
XUI_PORT="$(start_server xui "$TMP_DIR")"
STATIC_PORT="$(start_server static "$PUBLIC_DIR")"

PROVIDER_UPSTREAM_URL="http://127.0.0.1:${PROVIDER_PORT}/subscription"
XUI_SUBSCRIPTION_URL="http://127.0.0.1:${XUI_PORT}/sub/alice"
PUBLIC_BASE_URL="http://127.0.0.1:${STATIC_PORT}"
PROVIDER_TOKEN="provider_shared_token_1234567890abcd"
ALICE_TOKEN="alice_token_1234567890abcdefghijkl"

cat >"$CONFIG_PATH" <<EOF
provider:
  upstream_url: "$PROVIDER_UPSTREAM_URL"
  shared_token: "$PROVIDER_TOKEN"
  refresh_interval_seconds: 900

public:
  base_url: "$PUBLIC_BASE_URL"
  output_dir: "$PUBLIC_DIR"

render:
  profile_update_interval_seconds: 3600
  provider_update_interval_seconds: 900
  healthcheck_url: "$PUBLIC_BASE_URL/healthz"
  healthcheck_interval_seconds: 15
  healthcheck_timeout_milliseconds: 3000
  healthcheck_max_failed_times: 2
  healthcheck_tolerance_milliseconds: 50
  healthcheck_lazy: true
EOF

cat >"$USERS_PATH" <<EOF
users:
  alice:
    token: "$ALICE_TOKEN"
    xui_subscription: "$XUI_SUBSCRIPTION_URL"
EOF

SUBCTL=(env "PYTHONPATH=$REPO_ROOT/src${PYTHONPATH:+:$PYTHONPATH}" "$PYTHON_BIN" -m subctl.cli)

"${SUBCTL[@]}" --config "$CONFIG_PATH" --users "$USERS_PATH" --state-dir "$STATE_DIR" --output-dir "$PUBLIC_DIR" refresh-provider
"${SUBCTL[@]}" --config "$CONFIG_PATH" --users "$USERS_PATH" --state-dir "$STATE_DIR" --output-dir "$PUBLIC_DIR" render --all

YAML_URL="$PUBLIC_BASE_URL/s/${ALICE_TOKEN}.yaml"
RAW_URL="$PUBLIC_BASE_URL/s/${ALICE_TOKEN}.raw"
PUBLIC_PROVIDER_URL="$PUBLIC_BASE_URL/feeds/provider/${PROVIDER_TOKEN}"
DOWNLOADED_YAML="$TMP_DIR/alice.yaml"
DOWNLOADED_RAW="$TMP_DIR/alice.raw"
DOWNLOADED_PROVIDER="$TMP_DIR/provider.feed"

fetch_url "$YAML_URL" "$DOWNLOADED_YAML"
fetch_url "$RAW_URL" "$DOWNLOADED_RAW"
fetch_url "$PUBLIC_PROVIDER_URL" "$DOWNLOADED_PROVIDER"

"$PYTHON_BIN" - "$DOWNLOADED_PROVIDER" <<'PY'
import base64
import sys

provider_path = sys.argv[1]
data = open(provider_path, "rb").read().strip()
decoded = base64.b64decode(data, validate=True).decode("utf-8")
if "provider-node" not in decoded:
    raise SystemExit("public provider feed does not contain the mock provider node")
PY

if ! grep -F "url: $PUBLIC_PROVIDER_URL" "$DOWNLOADED_YAML" >/dev/null; then
  echo "YAML does not reference the public provider feed URL" >&2
  exit 1
fi

if grep -F "$PROVIDER_UPSTREAM_URL" "$DOWNLOADED_YAML" >/dev/null; then
  echo "YAML leaked the upstream provider URL" >&2
  exit 1
fi

"$PYTHON_BIN" - "$DOWNLOADED_RAW" <<'PY'
import base64
import sys

raw_path = sys.argv[1]
data = open(raw_path, "rb").read().strip()
decoded = base64.b64decode(data, validate=True).decode("utf-8")
if "PRIVATE%20%7C%20alice-node" not in decoded:
    raise SystemExit("raw output is missing the private node prefix")
if "PROVIDER%20%7C%20provider-node" not in decoded:
    raise SystemExit("raw output is missing the provider node prefix")
PY

echo "smoke-local: ok"
