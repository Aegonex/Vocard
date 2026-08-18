#!/usr/bin/env node
/*
 * Verify that Lavalink can actually STREAM YouTube audio, and report which
 * innertube client managed it.
 *
 * Why this exists: TrackStartEvent fires the moment a track is assigned to a
 * player, long before the source manager resolves a stream, and a player with
 * no voice client attached never produces frames — so both of the obvious
 * signals lie, and one of them already signed off a broken deploy. This drives
 * the youtube-source plugin's own REST route (GET /youtube/stream/{videoId}),
 * which runs the real client chain and pipes audio back, then counts bytes.
 *
 * Clients are tried one at a time rather than letting the plugin pick, because
 * the default order starts with WEB, whose SABR formats currently blow up this
 * route with an NPE even for videos that play fine elsewhere.
 *
 * Lavalink only listens on Railway's private network, so run it from inside a
 * container in the same project:
 *   { echo 'cat > /tmp/v.mjs <<"EOS"'; cat tools/verify-playback.mjs; echo EOS; \
 *     echo 'node /tmp/v.mjs dQw4w9WgXcQ'; } | railway ssh -s webpo-generator -- sh
 */
const BASE = process.env.LAVALINK_URL || 'http://lavalink.railway.internal:2333';
const AUTH = process.env.LAVALINK_PASSWORD || 'youshallnotpass';
const WANT_BYTES = Number(process.env.WANT_BYTES || 200000); // ≈ a few seconds of audio
const TIMEOUT_MS = Number(process.env.TIMEOUT_MS || 40000);
const CLIENTS = (process.env.CLIENTS || 'MWEB,TVHTML5_SIMPLY,MUSIC,ANDROID_MUSIC,IOS,ANDROID,TV,WEB,ANDROID_VR,WEBEMBEDDED').split(',');

const VIDEOS = process.argv.slice(2);
if (!VIDEOS.length) {
  console.error('usage: verify-playback.mjs <videoId> [...]   (raw 11-char ids)');
  process.exit(64);
}

async function tryClient(videoId, client) {
  const abort = new AbortController();
  const timer = setTimeout(() => abort.abort(), TIMEOUT_MS);
  const started = Date.now();
  try {
    const res = await fetch(`${BASE}/youtube/stream/${videoId}?withClient=${client}`, {
      headers: { Authorization: AUTH },
      signal: abort.signal,
    });
    if (!res.ok) {
      const body = await res.text().catch(() => '');
      let message = body.replace(/\s+/g, ' ').slice(0, 90);
      try { message = JSON.parse(body).message.slice(0, 90); } catch {}
      return { ok: false, why: `${res.status} ${message}` };
    }
    let bytes = 0;
    for await (const chunk of res.body) {
      bytes += chunk.length;
      if (bytes >= WANT_BYTES) break; // proof in hand, don't pull the whole track
    }
    abort.abort();
    if (bytes >= WANT_BYTES) return { ok: true, bytes, ms: Date.now() - started };
    return { ok: false, why: `stream ended after ${bytes} bytes` };
  } catch (err) {
    return { ok: false, why: err.name === 'AbortError' ? `timeout ${TIMEOUT_MS}ms` : String(err.message || err).slice(0, 90) };
  } finally {
    clearTimeout(timer);
  }
}

const results = [];
for (const videoId of VIDEOS) {
  const failures = [];
  let win = null;
  for (const client of CLIENTS) {
    const attempt = await tryClient(videoId, client);
    if (attempt.ok) { win = { client, ...attempt }; break; }
    failures.push(`${client}: ${attempt.why}`);
  }
  if (win) {
    console.log(`PASS  ${videoId.padEnd(13)} via ${win.client} — ${win.bytes} bytes in ${win.ms}ms`);
  } else {
    console.log(`FAIL  ${videoId.padEnd(13)} all ${CLIENTS.length} clients failed`);
    for (const line of failures) console.log(`        ${line}`);
  }
  results.push(Boolean(win));
}

const passed = results.filter(Boolean).length;
console.log(`\n${passed}/${results.length} streamed`);
process.exit(passed === results.length ? 0 : 1);
