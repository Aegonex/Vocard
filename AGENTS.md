# Working on this deployment

This is a fork of [Vocard](https://github.com/ChocoMeow/Vocard) running as a
fleet on Railway. Upstream's README covers the bot itself; this file covers what
is different here, and it is mostly about one thing: **YouTube blocks Railway's
addresses, and getting audio playing again required changes in three places.**

## What runs where

Everything below lives in one Railway project, `JED89`, environment `bot`
(project `4ab3f390-380a-4627-9a4a-39ce6ad7428a`, env
`f2116409-1b65-4731-ac25-02ff3cc87572`).

| Service | What it is |
|---|---|
| ~14 bot services (HT99, 789PG, van1, IOS123, milkbox, goya, …) | This repo. Each is one Discord bot, all sharing one Lavalink. |
| `Lavalink` | `lavalink/` — Lavalink 4.2.2 plus a YouTube plugin we build from source. |
| `webpo-generator` | poToken minter. Kept around mainly as a toolbox container: Lavalink listens on the private network only, so diagnostics have to run from inside the project. |

Off Railway, on a Vultr VPS (`149.28.133.240`, Ubuntu 24.04, root SSH): the **innertube
relay**, in `/opt/innertube-relay`, built from `relay/` in this repo.

Bots reach Lavalink at `lavalink.railway.internal:2333`. Railway's private
network is IPv6-only, so never force the JVM to IPv4 — it would cut the fleet off
from Lavalink.

## Why the relay exists

YouTube refuses innertube `/player` from Railway's address range with
`LOGIN_REQUIRED — "Sign in to confirm you're not a bot"`. This is not fixable
with credentials: poTokens, OAuth and every innertube client were tried, and the
identical request from an unflagged address succeeds *with no credentials at
all*. Bare datacenter addresses elsewhere (the Vultr box's own IP) are flagged
the same way.

Two facts make the fix cheap:

- Media delivery is not blocked, only the API.
- A stream URL still serves a full track when fetched from a different address,
  **provided its poToken was minted beside it**. Mixing identities fails.

So the API calls — a few KB per track — leave through the relay on a ProtonVPN
exit, and the audio still downloads straight from Railway. Only when a specific
video refuses that split does the audio detour through the relay too.

## The three moving parts

**1. `relay/`** — a Node service on the VPS.

- `/p/<host>/<path>` replays a request against a whitelisted YouTube host.
- `/generate` proxies poToken minting to a local webpo-generator, so the token
  and the player response share one identity.
- `/m?url=…` streams media, used only as a fallback.
- `/healthz` reports the egress address — the quickest way to tell whether the
  VPN is actually up.

The VPN runs **inside a gluetun container**, with the relay and minter joined to
its network namespace. Do not move it to the host: `wg-quick` installs policy
rules ahead of anything you add by hand, and putting ProtonVPN on the host took
sshd and the tunnel down together with no way back in short of the Vultr console.

**2. The YouTube plugin** — built from source in `lavalink/Dockerfile`, pinned by
`ARG YTS_COMMIT` to a commit on
[`Aegonex/youtube-source`](https://github.com/Aegonex/youtube-source), branch
`feat/innertube-relay`. That fork sits on top of upstream PR #229 (remote
poToken + SABR support) and adds:

- `plugins.youtube.remoteInnertube.url` / `.pass`, which rewrites innertube
  requests to `<relay>/p/<host>/<path>` as the *last* step of
  `YoutubeHttpContextFilter.onRequest` — everything before it matches on the
  original YouTube host and would stop firing once the URI changes. Media hosts
  are never rewritten here.
- A one-shot retry through `<relay>/m` when a media fetch is refused with 403 or
  the edge is unroutable. HTTP contexts are pooled per thread and outlive a
  track, so the anti-loop marker is cleared whenever a fresh request starts;
  leaving it set silently disables the fallback after its first use.

To change the plugin: edit the fork, push, build it (`docker run --rm -v
$PWD:/src -w /src gradle:8.10-jdk17 gradle :plugin:build -x test --no-daemon`),
then bump `ARG YTS_COMMIT` here and push. Railway rebuilds on changes under
`lavalink/**`.

**3. Railway variables on `Lavalink`** — none of this lives in the repo, and the
service silently regresses if they are lost:

- `PLUGINS_YOUTUBE_REMOTEINNERTUBE_URL` / `_PASS` — the relay.
- `PLUGINS_YOUTUBE_REMOTEPOT_URL` / `_PASS` — the same relay; the plugin sends
  this secret as `Authorization`, which is why the relay accepts either that or
  `X-Relay-Auth`.
- `PLUGINS_YOUTUBE_CLIENTS_0..9` — order matters for *searching* as much as
  playback, see below.
- `PLUGINS_YOUTUBE_REMOTECIPHER_URL` — public cipher server, `cipher.kikkia.dev`.
- `PLUGINS_YOUTUBE_OAUTH_*` — burner account, still configured, currently
  incidental.

## Verifying a change

**Do not trust playback events.** `TrackStartEvent` fires when a track is
assigned, before any stream is resolved, and a player with no voice client never
advances its position. Both signals report success while every client is
failing; that is how a broken deploy got signed off once here.

Use `tools/verify-playback.mjs`, which drives the plugin's own stream route and
counts audio bytes. Lavalink is private-network only, so run it from inside the
project:

```sh
{ echo 'cat > /tmp/v.mjs <<EOS'; cat tools/verify-playback.mjs; echo EOS;
  echo 'node /tmp/v.mjs dQw4w9WgXcQ <more ids>'; } \
| railway ssh -p <project> -e <env> -s webpo-generator -- sh
```

Two caveats learned the hard way: it tries every client per video and will
throttle itself on long batches, so results flap between runs — judge by batches
and by the player path, never by one video. And **always test `ytsearch:` too**,
not just direct video IDs: the plugin asks only the *first* client when
searching, and an order that plays fine can return zero results.

## When playback breaks

Read the Lavalink logs first and match the symptom:

| What the log says | What it means |
|---|---|
| `LOGIN_REQUIRED`, "Sign in to confirm you're not a bot" | The exit address is flagged. Switch Proton servers: `/opt/innertube-relay/switch-vpn.sh <new.conf>`. No Lavalink redeploy needed. |
| `Not success status code: 403` on media | The media fallback is not working. Check the relay is reachable and that the retry marker logic is intact. |
| `Invalid status code for player api response: 400` | YouTube retired that client's version. Bump it in the fork. |
| "The page needs to be reloaded" | A request went out without a signature timestamp. |
| Search returns nothing, playback fine | The first client in the order cannot parse search results. Reorder. |

`relay/README.md` has the relay's own runbook.

## Access notes

- Railway CLI is authenticated, but **pass `-p/-e/-s` explicitly** — the local
  link points at a deleted environment. Backgrounded `railway` commands produce
  no output on this machine (the token is keychain-gated); poll in the
  foreground instead.
- `railway ssh` needs the dedicated key registered as `vocard-claude`
  (`~/.ssh/railway_ed25519`); the default key belongs to another account and the
  CLI will not fall back to it.
- Setting a *changed* variable triggers a redeploy; setting an unchanged one does
  not, and deleting one is unreliable — bump a marker variable to force it.
- `railway service redeploy` clones the previous deployment and ignores new
  configuration. Push or set a variable instead.

## Known fragility

- The relay is a single point of failure for the whole fleet.
- It is exposed through a Cloudflare **quick** tunnel, which hands out a new
  hostname whenever `cloudflared` restarts — at which point the bots are pointing
  at a dead address. Moving to a named tunnel needs a Cloudflare account.
- The plugin fork is unmerged. If upstream ships a release that handles this,
  prefer it.
