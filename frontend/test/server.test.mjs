import assert from "node:assert/strict";
import { createServer, request as httpRequest } from "node:http";
import { mkdtemp, mkdir, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import { test } from "node:test";
import { gunzipSync } from "node:zlib";
import { createFrontendServer } from "../server.mjs";

async function listen(server) {
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  return server.address().port;
}

function get(port, pathname, headers = {}) {
  return new Promise((resolve, reject) => {
    const request = httpRequest({ hostname: "127.0.0.1", port, path: pathname, headers }, (response) => {
      const chunks = [];
      response.on("data", (chunk) => chunks.push(chunk));
      response.on("end", () => resolve({ status: response.statusCode, headers: response.headers,
        body: Buffer.concat(chunks) }));
    });
    request.on("error", reject);
    request.end();
  });
}

test("host frontend serves the built bundle and forwards API requests", async (t) => {
  const temp = await mkdtemp(path.join(tmpdir(), "tongling-front-"));
  t.after(() => rm(temp, { recursive: true, force: true }));
  await mkdir(path.join(temp, "assets"));
  await writeFile(path.join(temp, "index.html"), "<html>TongLing</html>");
  await writeFile(path.join(temp, "assets", "app.js"), "const greeting = 'TongLing';\n".repeat(100));
  const api = createServer((request, response) => {
    response.writeHead(200, { "content-type": "application/json" });
    response.end(JSON.stringify({ path: request.url, host: request.headers.host }));
  });
  const apiPort = await listen(api);
  t.after(() => api.close());
  const front = createFrontendServer({ role: "host", apiPort, distDir: temp });
  const frontPort = await listen(front);
  t.after(() => front.close());

  const page = await get(frontPort, "/");
  assert.equal(page.status, 200);
  assert.match(page.body.toString(), /TongLing/);
  assert.equal(page.headers["cache-control"], "no-cache");
  const asset = await get(frontPort, "/assets/app.js", { "accept-encoding": "gzip" });
  assert.equal(asset.status, 200);
  assert.equal(asset.headers["content-encoding"], "gzip");
  assert.match(gunzipSync(asset.body).toString(), /TongLing/);
  assert.equal(asset.headers["cache-control"], "public, max-age=31536000, immutable");
  const proxied = await get(frontPort, "/api/health?check=1");
  assert.deepEqual(JSON.parse(proxied.body), { path: "/api/health?check=1", host: `127.0.0.1:${frontPort}` });
  assert.equal((await get(frontPort, "/assets/%2e%2e/index.html")).status, 404);
});

test("client frontend accepts only the active tunnel host", async (t) => {
  const temp = await mkdtemp(path.join(tmpdir(), "tongling-front-"));
  t.after(() => rm(temp, { recursive: true, force: true }));
  await writeFile(path.join(temp, "index.html"), "client page");
  const urlFile = path.join(temp, "url.txt");
  await writeFile(urlFile, "https://example.ngrok-free.dev");
  const front = createFrontendServer({ role: "client", apiPort: 8001, distDir: temp, urlFile });
  const port = await listen(front);
  t.after(() => front.close());

  assert.equal((await get(port, "/", { host: "example.ngrok-free.dev" })).status, 200);
  assert.equal((await get(port, "/", { host: "wrong.example.com" })).status, 400);
  assert.equal((await get(port, "/settings", { host: "example.ngrok-free.dev" })).status, 404);
});
