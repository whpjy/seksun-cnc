import { createServer, request } from 'node:http';
import { createConnection } from 'node:net';
import { createReadStream } from 'node:fs';
import { stat } from 'node:fs/promises';
import { extname, resolve, sep } from 'node:path';

const root = resolve('public');
const harness = new URL(process.env.PROCESS_HARNESS_TARGET || 'http://harness:3081');
const cnc = new URL(process.env.PROCESS_CNC_TARGET || 'http://api:8090');
const publicPort = process.env.PROCESS_PUBLIC_PORT || '3082';
// The Harness trusts its local public endpoint; credentials stay in this server.
const harnessHost = '127.0.0.1:3081';
let cookie = '';
let authenticatedAt = 0;
let authentication;

function loginRequest(url) {
  return new Promise((resolveRequest, reject) => {
    const req = request(url, { headers: { host: harnessHost } }, res => {
      res.resume();
      resolveRequest({ status: res.statusCode, headers: res.headers });
    });
    req.setTimeout(10000, () => req.destroy(new Error('Harness login timed out')));
    req.on('error', reject);
    req.end();
  });
}

async function authenticate(force = false) {
  if (!force && cookie && Date.now() - authenticatedAt < 10000) return cookie;
  if (!authentication) {
    authentication = (async () => {
      const bootstrap = await loginRequest(new URL('/', harness));
      const location = bootstrap.headers.location;
      if (bootstrap.status !== 303 || !location) throw new Error('Harness is not ready');
      const loginUrl = new URL(location, harness);
      if (loginUrl.origin !== harness.origin) throw new Error('Unexpected Harness login redirect');
      const login = await loginRequest(loginUrl);
      const nextCookie = (login.headers['set-cookie']?.[0] || '').split(';', 1)[0];
      if (login.status !== 303 || !nextCookie) throw new Error('Harness authentication failed');
      cookie = nextCookie;
      authenticatedAt = Date.now();
      return cookie;
    })().finally(() => { authentication = undefined; });
  }
  return authentication;
}

function upstreamHeaders(headers, target, authCookie) {
  const next = { ...headers, host: target.host };
  if (authCookie) {
    next.host = harnessHost;
    next.origin = `http://${harnessHost}`;
    next.cookie = authCookie;
    delete next.referer;
  }
  return next;
}

const mime = {
  '.html': 'text/html; charset=utf-8', '.js': 'text/javascript; charset=utf-8',
  '.mjs': 'text/javascript; charset=utf-8', '.wasm': 'application/wasm',
  '.css': 'text/css; charset=utf-8', '.json': 'application/json',
  '.svg': 'image/svg+xml', '.png': 'image/png', '.jpg': 'image/jpeg',
  '.ico': 'image/x-icon', '.woff2': 'font/woff2',
};

function isLocalRequest(req) {
  try {
    const host = new URL(`http://${req.headers.host}`);
    if (!['localhost', '127.0.0.1', '[::1]'].includes(host.hostname) || host.port !== publicPort) return false;
    if (req.headers['sec-fetch-site'] === 'cross-site') return false;
    return !req.headers.origin || new URL(req.headers.origin).host === host.host;
  } catch { return false; }
}

const server = createServer(async (req, res) => {
  if (!isLocalRequest(req)) { res.writeHead(403).end(); return; }
  try {
    const isHarness = req.url === '/harness' || req.url.startsWith('/harness/');
    if (isHarness || req.url.startsWith('/api/')) {
      const target = isHarness ? harness : cnc;
      const authCookie = isHarness ? await authenticate() : undefined;
      const upstream = request({
        hostname: target.hostname, port: target.port,
        path: isHarness ? req.url.replace(/^\/harness/, '') || '/' : req.url,
        method: req.method, headers: upstreamHeaders(req.headers, target, authCookie),
      }, response => {
        if (isHarness && response.statusCode === 401) authenticatedAt = 0;
        const headers = { ...response.headers };
        if (isHarness) delete headers['set-cookie'];
        res.writeHead(response.statusCode || 502, headers);
        response.pipe(res);
      });
      upstream.on('error', () => {
        if (!res.headersSent) res.writeHead(502);
        res.end('Backend unavailable');
      });
      req.on('aborted', () => upstream.destroy());
      res.on('close', () => upstream.destroy());
      req.pipe(upstream);
      return;
    }
    if (!['GET', 'HEAD'].includes(req.method)) { res.writeHead(405).end(); return; }
    const pathname = decodeURIComponent(new URL(req.url, 'http://localhost').pathname);
    const file = pathname === '/' ? resolve(root, 'process.html') : resolve(root, `.${pathname}`);
    if (!file.startsWith(root + sep)) { res.writeHead(403).end(); return; }
    const info = await stat(file).catch(() => undefined);
    if (!info?.isFile()) { res.writeHead(404).end(); return; }
    res.writeHead(200, {
      'content-type': mime[extname(file)] || 'application/octet-stream',
      'content-length': info.size,
      'cache-control': extname(file) === '.html' ? 'no-store' : 'no-cache',
    });
    if (req.method === 'HEAD') res.end();
    else createReadStream(file).on('error', () => res.destroy()).pipe(res);
  } catch {
    if (!res.headersSent) res.writeHead(503, { 'retry-after': '1' });
    res.end('Service is starting; retry in a moment.');
  }
});

server.on('upgrade', async (req, socket, head) => {
  socket.on('error', () => socket.destroy());
  if (!isLocalRequest(req) || !req.url.startsWith('/harness/')) { socket.destroy(); return; }
  try {
    const authCookie = await authenticate(true);
    if (socket.destroyed) return;
    const upstream = createConnection({ host: harness.hostname, port: Number(harness.port) }, () => {
      const headers = upstreamHeaders(req.headers, harness, authCookie);
      const lines = Object.entries(headers).flatMap(([name, value]) =>
        (Array.isArray(value) ? value : [value]).map(item => `${name}: ${item}`));
      upstream.write(`${req.method} ${req.url.replace(/^\/harness/, '')} HTTP/${req.httpVersion}\r\n${lines.join('\r\n')}\r\n\r\n`);
      if (head.length) upstream.write(head);
      socket.pipe(upstream).pipe(socket);
    });
    upstream.on('error', () => socket.destroy());
    socket.on('close', () => upstream.destroy());
    upstream.on('close', () => socket.destroy());
  } catch { socket.destroy(); }
});

server.listen(3082, '0.0.0.0', () => console.log('Process Studio listening on port 3082'));
for (const signal of ['SIGINT', 'SIGTERM']) {
  process.on(signal, () => { server.close(); setTimeout(() => process.exit(0), 1000).unref(); });
}
