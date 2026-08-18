/*
 * Innertube relay — forwards YouTube API calls from an IP YouTube still trusts.
 *
 * YouTube has flagged Railway's address range: every /player call from there
 * answers LOGIN_REQUIRED ("Sign in to confirm you're not a bot") no matter what
 * poToken, OAuth token or innertube client we send, while the same call from a
 * clean address succeeds with no credentials at all. Audio streaming is not
 * blocked, and a stream URL stays valid when fetched from a different address
 * as long as its poToken was minted alongside it — so only the API traffic,
 * a few KB per track, has to make the detour through here.
 *
 * Requests arrive as /p/<host>/<path>, are replayed verbatim against that host,
 * and the upstream response is passed back untouched. poToken minting is
 * proxied to the local webpo-generator so tokens carry this machine's identity,
 * which is what makes the resulting stream URLs usable from Railway.
 */
import http from 'node:http';
import { timingSafeEqual } from 'node:crypto';

const PORT = Number(process.env.PORT || 8081);
const HOST = process.env.HOST || '0.0.0.0';
const TOKEN = process.env.RELAY_TOKEN || '';
const WEBPO_URL = process.env.WEBPO_URL || 'http://127.0.0.1:8080';
const WEBPO_TOKEN = process.env.WEBPO_TOKEN || '';
const UPSTREAM_TIMEOUT_MS = Number(process.env.UPSTREAM_TIMEOUT_MS || 20000);

// Anything not on this list is refused: the relay must never become an open proxy.
// Media hosts are only reachable through /m, and only as a fallback: their URLs
// are usually fetched directly by whoever needs the audio.
const MEDIA_HOST_SUFFIX = '.googlevideo.com';

const ALLOWED_HOSTS = new Set([
  'youtubei.googleapis.com',
  'www.youtube.com',
  'm.youtube.com',
  'music.youtube.com',
  'www.youtube-nocookie.com',
  'oauth2.googleapis.com',
]);

// Headers that describe our hop, not the request being forwarded.
const STRIPPED_REQUEST_HEADERS = new Set([
  'host', 'connection', 'keep-alive', 'proxy-authenticate', 'proxy-authorization',
  'te', 'trailer', 'transfer-encoding', 'upgrade', 'content-length',
  'x-relay-auth', 'accept-encoding',
]);
const STRIPPED_RESPONSE_HEADERS = new Set([
  'connection', 'keep-alive', 'transfer-encoding', 'content-encoding', 'content-length',
]);

const stats = { forwarded: 0, minted: 0, media: 0, mediaBytes: 0, rejected: 0, upstreamErrors: 0, startedAt: Date.now() };

function matchesToken(given) {
  if (typeof given !== 'string') return false;
  const a = Buffer.from(given);
  const b = Buffer.from(TOKEN);
  return a.length === b.length && timingSafeEqual(a, b);
}

function authorized(req) {
  if (!TOKEN) return true; // unset token means the operator accepted an open relay
  // The plugin's poToken client sends its secret as Authorization, while relayed
  // innertube calls need that header left alone for YouTube's OAuth bearer — so
  // accept either, and only treat Authorization as ours when it actually matches.
  return matchesToken(req.headers['x-relay-auth']) || matchesToken(req.headers.authorization);
}

function send(res, status, payload) {
  const body = JSON.stringify(payload);
  res.writeHead(status, { 'Content-Type': 'application/json; charset=utf-8', 'Cache-Control': 'no-store' });
  res.end(body);
}

function readBody(req) {
  return new Promise((resolve, reject) => {
    const chunks = [];
    let size = 0;
    req.on('data', (chunk) => {
      size += chunk.length;
      if (size > 2 * 1024 * 1024) { reject(new Error('request body too large')); req.destroy(); return; }
      chunks.push(chunk);
    });
    req.on('end', () => resolve(Buffer.concat(chunks)));
    req.on('error', reject);
  });
}

async function forward(req, res, targetHost, targetPath) {
  if (!ALLOWED_HOSTS.has(targetHost)) {
    stats.rejected++;
    return send(res, 403, { error: 'host not allowed', host: targetHost });
  }

  const headers = {};
  for (const [name, value] of Object.entries(req.headers)) {
    if (!STRIPPED_REQUEST_HEADERS.has(name.toLowerCase())) headers[name] = value;
  }
  // Upstream must see the real destination, not our tunnel hostname.
  headers.host = targetHost;

  const body = req.method === 'GET' || req.method === 'HEAD' ? undefined : await readBody(req);
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), UPSTREAM_TIMEOUT_MS);

  try {
    const upstream = await fetch(`https://${targetHost}${targetPath}`, {
      method: req.method,
      headers,
      body,
      redirect: 'manual',
      signal: controller.signal,
    });

    const outHeaders = {};
    upstream.headers.forEach((value, name) => {
      if (!STRIPPED_RESPONSE_HEADERS.has(name.toLowerCase())) outHeaders[name] = value;
    });
    const buffer = Buffer.from(await upstream.arrayBuffer());
    stats.forwarded++;
    res.writeHead(upstream.status, outHeaders);
    res.end(buffer);
  } catch (err) {
    stats.upstreamErrors++;
    send(res, 502, { error: 'upstream request failed', detail: String(err.message || err) });
  } finally {
    clearTimeout(timer);
  }
}

async function mint(req, res) {
  const body = req.method === 'POST' ? await readBody(req) : Buffer.from('{}');
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), UPSTREAM_TIMEOUT_MS);
  try {
    const upstream = await fetch(`${WEBPO_URL}/generate`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', ...(WEBPO_TOKEN ? { Authorization: WEBPO_TOKEN } : {}) },
      body,
      signal: controller.signal,
    });
    const text = await upstream.text();
    stats.minted++;
    res.writeHead(upstream.status, { 'Content-Type': 'application/json; charset=utf-8' });
    res.end(text);
  } catch (err) {
    stats.upstreamErrors++;
    send(res, 502, { error: 'webpo-generator unreachable', detail: String(err.message || err) });
  } finally {
    clearTimeout(timer);
  }
}

/**
 * Streams a media URL from this machine's address.
 *
 * Some stream URLs answer 403 to anyone but the address that requested them, so
 * the caller falls back to here after a direct fetch is refused. The body is
 * piped straight through rather than buffered: tracks are megabytes, and holding
 * one per listener in memory would be a fine way to kill the box.
 */
async function media(req, res, target) {
  let url;
  try {
    url = new URL(target);
  } catch {
    return send(res, 400, { error: 'invalid url' });
  }

  if (url.protocol !== 'https:' || !url.hostname.endsWith(MEDIA_HOST_SUFFIX)) {
    stats.rejected++;
    return send(res, 403, { error: 'host not allowed', host: url.hostname });
  }

  const headers = {};
  // Range is the only request header that matters here, and it matters a lot:
  // the player seeks by asking for byte windows.
  if (req.headers.range) headers.range = req.headers.range;
  if (req.headers['user-agent']) headers['user-agent'] = req.headers['user-agent'];

  try {
    const upstream = await fetch(url, { method: req.method === 'HEAD' ? 'HEAD' : 'GET', headers, redirect: 'follow' });
    const outHeaders = {};
    upstream.headers.forEach((value, name) => {
      if (!STRIPPED_RESPONSE_HEADERS.has(name.toLowerCase())) outHeaders[name] = value;
    });
    if (upstream.headers.get('content-length')) outHeaders['content-length'] = upstream.headers.get('content-length');
    res.writeHead(upstream.status, outHeaders);
    stats.media++;

    if (!upstream.body) return res.end();
    for await (const chunk of upstream.body) {
      stats.mediaBytes += chunk.length;
      if (!res.write(chunk)) await new Promise((resolve) => res.once('drain', resolve));
    }
    res.end();
  } catch (err) {
    stats.upstreamErrors++;
    if (!res.headersSent) send(res, 502, { error: 'media fetch failed', detail: String(err.message || err) });
    else res.destroy();
  }
}

// Egress address is the whole point of this box, so make it cheap to check.
let egressCache = { ip: null, at: 0 };
async function egress() {
  if (egressCache.ip && Date.now() - egressCache.at < 60000) return egressCache.ip;
  try {
    const ip = (await fetch('https://api.ipify.org', { signal: AbortSignal.timeout(5000) }).then((r) => r.text())).trim();
    egressCache = { ip, at: Date.now() };
    return ip;
  } catch {
    return null;
  }
}

const server = http.createServer(async (req, res) => {
  try {
    const path = req.url || '/';

    if (path === '/healthz') {
      return send(res, 200, {
        ok: true,
        egressIp: await egress(),
        uptimeSeconds: Math.round((Date.now() - stats.startedAt) / 1000),
        ...stats,
      });
    }

    if (!authorized(req)) {
      stats.rejected++;
      return send(res, 401, { error: 'unauthorized' });
    }

    if (path === '/generate' || path.startsWith('/generate?')) return await mint(req, res);

    if (path.startsWith('/m?')) {
      const target = new URLSearchParams(path.slice(3)).get('url');
      if (!target) return send(res, 400, { error: 'expected /m?url=<encoded url>' });
      return await media(req, res, target);
    }

    if (path.startsWith('/p/')) {
      const rest = path.slice(3);
      const slash = rest.indexOf('/');
      // A bare host is legitimate: the plugin fetches https://www.youtube.com with
      // no path at all to scrape its client config.
      if (slash === 0) return send(res, 400, { error: 'expected /p/<host>/<path>' });
      const host = slash < 0 ? rest : rest.slice(0, slash);
      const target = slash < 0 ? '/' : rest.slice(slash);
      return await forward(req, res, host, target);
    }

    send(res, 404, { error: 'not found' });
  } catch (err) {
    send(res, 500, { error: 'relay failure', detail: String(err.message || err) });
  }
});

server.listen(PORT, HOST, async () => {
  console.log(`innertube relay listening on http://${HOST}:${PORT} (egress ${await egress()})`);
  if (!TOKEN) console.warn('RELAY_TOKEN is unset — the relay will accept unauthenticated callers');
});
