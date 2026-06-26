// Mini servidor estático del dashboard de AeroIntel (local). Sirve output/ en :8200.
import { createServer } from 'node:http';
import { readFile } from 'node:fs/promises';
import { join, extname, normalize } from 'node:path';

const ROOT = '/Users/usuario/Desktop/Cloude Code/aerointel/output';
const PORT = 8200;
const MIME = {
  '.html': 'text/html; charset=utf-8', '.json': 'application/json; charset=utf-8',
  '.md': 'text/plain; charset=utf-8', '.css': 'text/css', '.js': 'text/javascript',
};

createServer(async (req, res) => {
  let p = decodeURIComponent(new URL(req.url, 'http://x').pathname);
  if (p === '/' || p === '') p = '/dashboard.html';
  const fp = normalize(join(ROOT, p));
  if (!fp.startsWith(ROOT)) { res.writeHead(403).end('forbidden'); return; }
  try {
    const data = await readFile(fp);
    res.writeHead(200, { 'Content-Type': MIME[extname(fp)] || 'application/octet-stream', 'Cache-Control': 'no-store' });
    res.end(data);
  } catch { res.writeHead(404, { 'Content-Type': 'text/plain' }).end('not found'); }
}).listen(PORT, () => console.log(`AeroIntel dashboard en http://localhost:${PORT}/`));
