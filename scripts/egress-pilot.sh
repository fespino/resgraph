#!/usr/bin/env bash
# Egress pilot (D35): measures, on THIS host, whether a compose-level
# mechanism can block store-container egress while keeping host->container
# published ports working. Two candidate mechanisms, both probed in both
# directions with bare exit codes. A control adopted without this pilot
# is a control that may control nothing (the masquerade case, on Docker
# Desktop, is exactly that).
set -u

WORK=$(mktemp -d)
COMPOSE="$WORK/compose.yaml"
PORT=16379
trap 'docker compose -f "$COMPOSE" down >/dev/null 2>&1; rm -rf "$WORK"' EXIT

inbound_probe() {
  python3 - <<PY
import socket
s = socket.socket()
s.settimeout(3)
try:
    s.connect(("127.0.0.1", $PORT))
    s.sendall(b"PING\r\n")
    ok = s.recv(16).startswith(b"+PONG")
    print("works" if ok else "no-reply")
except OSError:
    print("refused")
PY
}

egress_probe() {
  if docker compose -f "$COMPOSE" exec -T probe \
    timeout 5 wget -q -O /dev/null -T 4 http://example.com >/dev/null 2>&1; then
    echo "OPEN"
  else
    echo "blocked"
  fi
}

run_case() {
  local name="$1" network_block="$2"
  cat > "$COMPOSE" <<EOF
services:
  probe:
    image: redis:8-alpine
    ports: ["127.0.0.1:$PORT:6379"]
    networks: [noegress]
networks:
  noegress:
$network_block
EOF
  docker compose -f "$COMPOSE" up -d >/dev/null 2>&1
  sleep 2
  local inbound egress
  inbound=$(inbound_probe)
  egress=$(egress_probe)
  docker compose -f "$COMPOSE" down >/dev/null 2>&1
  printf "%-22s inbound(host->port): %-9s egress(container->net): %s\n" \
    "$name" "$inbound" "$egress"
}

echo "egress-pilot: $(docker version --format '{{.Server.Os}}/{{.Server.Arch}} {{.Server.Version}}' 2>/dev/null) on $(uname -s)"
run_case "masquerade-off" '    driver: bridge
    driver_opts:
      com.docker.network.bridge.enable_ip_masquerade: "false"'
run_case "internal-true" '    internal: true'
echo "a usable control needs: inbound works AND egress blocked"
