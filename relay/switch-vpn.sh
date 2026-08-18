#!/bin/sh
# Point the relay at a different ProtonVPN exit.
#
# Usage: ./switch-vpn.sh /path/to/downloaded-proton.conf
#
# Proton hands out one WireGuard config per server, so changing exit means
# swapping the peer details and restarting the tunnel. Only the containers that
# sit inside the VPN's network namespace need to come back with it; cloudflared
# stays on the host network and keeps its connection, which is what preserves
# the public hostname Lavalink is pointed at.
set -e

CONF="$1"
DIR="$(cd "$(dirname "$0")" && pwd)"
ENV_FILE="$DIR/.env"

if [ -z "$CONF" ] || [ ! -f "$CONF" ]; then
  echo "usage: $0 <proton-wireguard.conf>" >&2
  exit 64
fi

field() { awk -F'= *' "/^$1/ {print \$2; exit}" "$CONF" | tr -d ' \r'; }

PRIV=$(field PrivateKey)
PUB=$(field PublicKey)
ENDPOINT=$(field Endpoint)
ADDR=$(field Address | cut -d, -f1)

if [ -z "$PRIV" ] || [ -z "$PUB" ] || [ -z "$ENDPOINT" ] || [ -z "$ADDR" ]; then
  echo "config is missing PrivateKey/PublicKey/Endpoint/Address" >&2
  exit 65
fi

BEFORE=$(curl -s --max-time 10 http://127.0.0.1:8081/healthz | sed 's/.*"egressIp":"\([^"]*\)".*/\1/')

# Keep every non-WireGuard secret (relay and webpo tokens) exactly as it was.
grep -v '^WIREGUARD_' "$ENV_FILE" > "$ENV_FILE.new"
{
  echo "WIREGUARD_PRIVATE_KEY=$PRIV"
  echo "WIREGUARD_PUBLIC_KEY=$PUB"
  echo "WIREGUARD_ENDPOINT_IP=${ENDPOINT%%:*}"
  echo "WIREGUARD_ENDPOINT_PORT=${ENDPOINT##*:}"
  echo "WIREGUARD_ADDRESSES=$ADDR"
} >> "$ENV_FILE.new"
mv "$ENV_FILE.new" "$ENV_FILE"
chmod 600 "$ENV_FILE"

cd "$DIR"
docker compose up -d --force-recreate vpn webpo-generator relay

printf 'waiting for the tunnel'
i=0
while [ $i -lt 30 ]; do
  AFTER=$(curl -s --max-time 5 http://127.0.0.1:8081/healthz | sed 's/.*"egressIp":"\([^"]*\)".*/\1/')
  case "$AFTER" in
    ''|null|"$BEFORE") printf '.'; sleep 3; i=$((i + 1)) ;;
    *) echo; echo "egress is now $AFTER (was ${BEFORE:-unknown})"; exit 0 ;;
  esac
done

echo
echo "tunnel did not report a new egress address; check: docker compose logs vpn" >&2
exit 1
