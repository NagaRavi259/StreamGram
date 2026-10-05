// Tiny static file server with real byte-Range support, standing in for the bot's existing
// streaming endpoint for e2e tests. Serves everything under `webplayer/` at the repo-relative
// path it's requested with, plus the test fixture.
import http from "node:http";
import { createReadStream, statSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.join(__dirname, "..", ".."); // webplayer/

const MIME = { ".html": "text/html", ".mjs": "text/javascript", ".js": "text/javascript", ".webm": "video/webm" };

export function startRangeServer(port = 0) {
  const server = http.createServer((req, res) => {
    const urlPath = decodeURIComponent(req.url.split("?")[0]);
    const filePath = path.join(ROOT, urlPath);
    if (!filePath.startsWith(ROOT)) {
      res.writeHead(403).end();
      return;
    }
    let stat;
    try {
      stat = statSync(filePath);
    } catch {
      res.writeHead(404).end("not found");
      return;
    }
    const ext = path.extname(filePath);
    const contentType = MIME[ext] || "application/octet-stream";
    const range = req.headers.range;
    if (range) {
      const m = /^bytes=(\d*)-(\d*)$/.exec(range);
      if (!m) {
        res.writeHead(416, { "Content-Range": `bytes */${stat.size}` }).end();
        return;
      }
      const start = m[1] ? parseInt(m[1], 10) : stat.size - parseInt(m[2], 10);
      const end = m[2] && m[1] ? parseInt(m[2], 10) : stat.size - 1;
      if (start > end || start >= stat.size) {
        res.writeHead(416, { "Content-Range": `bytes */${stat.size}` }).end();
        return;
      }
      res.writeHead(206, {
        "Content-Type": contentType,
        "Content-Range": `bytes ${start}-${end}/${stat.size}`,
        "Content-Length": end - start + 1,
        "Accept-Ranges": "bytes",
      });
      createReadStream(filePath, { start, end }).pipe(res);
    } else {
      res.writeHead(200, {
        "Content-Type": contentType,
        "Content-Length": stat.size,
        "Accept-Ranges": "bytes",
      });
      createReadStream(filePath).pipe(res);
    }
  });
  return new Promise((resolve) => {
    server.listen(port, "127.0.0.1", () => resolve(server));
  });
}

if (import.meta.url === `file://${process.argv[1]}`) {
  const server = await startRangeServer(8099);
  console.log(`range server listening on http://127.0.0.1:${server.address().port}`);
}
