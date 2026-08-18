# Innertube relay

Forwards YouTube API calls from an IP YouTube still trusts.

## Why

YouTube has flagged Railway's address range. Every innertube `/player` call from
there answers `LOGIN_REQUIRED — "Sign in to confirm you're not a bot"`, and no
combination of poToken, OAuth token or innertube client changes that; the same
call from a clean address succeeds with no credentials at all.

Audio streaming is *not* blocked. A stream URL keeps working when fetched from a
different address, provided its poToken was minted alongside it — verified by
pulling a full track from one machine using a URL minted on another. So only the
API traffic (a few KB per track) needs to leave through the clean IP, while the
audio, which is all of the bandwidth, still comes straight out of Railway.

## Layout

- `server.js` — the relay. `/p/<host>/<path>` replays a request against a
  whitelisted YouTube host; `/generate` proxies poToken minting to the local
  webpo-generator so tokens carry this machine's identity; `/healthz` reports the
  egress IP so you can confirm the VPN is actually up.
- `compose.yml` — relay + webpo-generator + a Cloudflare quick tunnel, all on the
  host network so VPN routing applies to them.

## Setup

1. Put the WireGuard details from the Proton config into `.env` next to
   `compose.yml`: `WIREGUARD_PRIVATE_KEY`, `WIREGUARD_PUBLIC_KEY` (the `[Peer]`
   one), `WIREGUARD_ENDPOINT_IP`, `WIREGUARD_ADDRESSES` (the IPv4 `Address`),
   plus `RELAY_TOKEN` and `WEBPO_TOKEN`.
2. `docker compose up -d`
3. `curl -s localhost:8081/healthz` — `egressIp` must be the VPN address, not the
   host's own. If it is not, container traffic is bypassing the tunnel and the
   relay is worthless.

Do **not** run ProtonVPN on the host instead: it claims the default route from
every process, which drops sshd's replies and cloudflared's tunnel at the same
time and leaves no way back into the box.
4. Read the tunnel hostname out of `docker compose logs cloudflared`, then point
   Lavalink at it with `PLUGINS_YOUTUBE_REMOTEINNERTUBE_URL` /
   `PLUGINS_YOUTUBE_REMOTEINNERTUBE_PASS` and set `PLUGINS_YOUTUBE_REMOTEPOT_URL`
   to the same host.

A quick tunnel hands out a new hostname on every restart; move to a named tunnel
once this proves itself, otherwise a relay restart silently strands the bots.

## Changing exit server

If YouTube starts refusing this exit the way it refuses datacenter addresses,
download another WireGuard config from Proton and hand it to the switcher:

    ./switch-vpn.sh ~/vocard-relay-XX-99.conf

It swaps the peer details, restarts only the containers inside the VPN's network
namespace, and prints the new egress address. Lavalink needs no redeploy:
cloudflared stays up on the host network, so the hostname it is pointed at
survives. Confirm with `tools/verify-playback.mjs` afterwards.

Not every playback failure is the exit address, though — YouTube also breaks
things by retiring client versions and by changing how streams are served, and
those look nothing like a refusal.
