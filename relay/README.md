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

1. Bring up ProtonVPN on the host and confirm the egress IP changed.
2. `RELAY_TOKEN=$(openssl rand -hex 24) WEBPO_TOKEN=$(openssl rand -hex 24) docker compose up -d`
3. `curl -s localhost:8081/healthz` — `egressIp` must be the VPN address, not the
   host's own. If it is not, container traffic is bypassing the tunnel and the
   relay is worthless.
4. Read the tunnel hostname out of `docker compose logs cloudflared`, then point
   Lavalink at it with `PLUGINS_YOUTUBE_REMOTEINNERTUBE_URL` /
   `PLUGINS_YOUTUBE_REMOTEINNERTUBE_PASS` and set `PLUGINS_YOUTUBE_REMOTEPOT_URL`
   to the same host.

A quick tunnel hands out a new hostname on every restart; move to a named tunnel
once this proves itself, otherwise a relay restart silently strands the bots.
