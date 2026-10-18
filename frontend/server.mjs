"use strict";

import { createServer, request as httpRequest } from "node:http";
import { createReadStream } from "node:fs";
import { readFile, realpath, stat } from "node:fs/promises";
import { createGzip } from "node:zlib";
import path from "node:path";
import { fileURLToPath } from "node:url";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.resolve(HERE, "..");
const TYPES = {
  ".html": "text/html; charset=utf-8",
  ".js": "text/javascript; charset=utf-8",
  ".css": "text/css; charset=utf-8",
  ".json": "application/json; charset=utf-8",
  ".svg": "image/svg+xml",
  ".png": "image/png",
  ".jpg": "image/jpeg",
  ".jpeg": "image/jpeg",
  ".webp": "image/webp",
  ".ico": "image/x-icon",
  ".woff": "font/woff",
  ".woff2": "font/woff2",
};

function sendJson(response, status, body) {
  response.writeHead(status, {
    "content-type": "application/json; charset=utf-8",
    "cache-control": "no-store",
  });
  response.end(JSON.stringify(body));
}

async function allowedHost(hostHeader, role, urlFile) {
  let hostname;
  try {
    hostname = new URL(`http://${hostHeader}`).hostname.toLowerCase();
  } catch {
    return false;
  }
  if (hostname === "localhost" || hostname === "127.0.0.1" || hostname === "[::1]") {
    return true;
  }
  if (role !== "client") return false;
  try {
    return hostname === new URL((await readFile(urlFile, "utf8")).trim()).hostname.toLowerCase();
  } catch {
    return false;
  }
}

function proxyApi(request, response, apiPort) {
  const upstream = httpRequest({
    hostname: "127.0.0.1",
    port: apiPort,
    method: request.method,
    path: request.url,
    headers: request.headers,
  }, (received) => {
    response.writeHead(received.statusCode ?? 502, received.headers);
    received.pipe(response);
  });
  upstream.on("error", () => {
    if (!response.headersSent) sendJson(response, 502, { detail: "後端暫時無法連線" });
    else response.destroy();
  });
  request.on("aborted", () => upstream.destroy());
  request.pipe(upstream);
}

async function serveStatic(request, response, pathname, distDir) {
  if (request.method !== "GET" && request.method !== "HEAD") {
    sendJson(response, 405, { detail: "不支援此操作" });
    return;
  }
  const index = pathname === "/" || pathname === "/index.html";
  if (!index && !pathname.startsWith("/assets/")) {
    sendJson(response, 404, { detail: "找不到頁面" });
    return;
  }
  let relative;
  try {
    relative = index ? "index.html" : decodeURIComponent(pathname.slice(1));
  } catch {
    sendJson(response, 400, { detail: "無效的網址" });
    return;
  }
  if (relative.split(/[\\/]/).some((segment) => segment === "." || segment === "..")) {
    sendJson(response, 404, { detail: "找不到頁面" });
    return;
  }
  const dist = path.resolve(distDir);
  const candidate = path.resolve(dist, relative);
  if (!candidate.startsWith(`${dist}${path.sep}`)) {
    sendJson(response, 404, { detail: "找不到頁面" });
    return;
  }
  try {
    const [actual, info] = await Promise.all([realpath(candidate), stat(candidate)]);
    if (!actual.startsWith(`${dist}${path.sep}`) || !info.isFile()) {
      sendJson(response, 404, { detail: "找不到頁面" });
      return;
    }
    const type = TYPES[path.extname(actual).toLowerCase()] ?? "application/octet-stream";
    const headers = {
      "content-type": type,
      "cache-control": index ? "no-cache" : "public, max-age=31536000, immutable",
      "x-content-type-options": "nosniff",
      "vary": "Accept-Encoding",
    };
    const compress = info.size >= 1024 && /\bgzip\b/.test(request.headers["accept-encoding"] ?? "") &&
      /(?:text\/|javascript|json|svg)/.test(type);
    if (compress) headers["content-encoding"] = "gzip";
    else headers["content-length"] = info.size;
    response.writeHead(200, headers);
    if (request.method === "HEAD") { response.end(); return; }
    const stream = createReadStream(actual);
    stream.on("error", () => response.destroy());
    if (compress) stream.pipe(createGzip({ level: 5 })).pipe(response);
    else stream.pipe(response);
  } catch {
    sendJson(response, 404, { detail: "找不到頁面" });
  }
}

export function createFrontendServer({ role, apiPort, distDir = path.join(HERE, "dist"),
                                        urlFile = path.join(ROOT, "data", "client_gateway_url.txt") }) {
  if (!(["host", "client"].includes(role) && Number.isInteger(apiPort) && apiPort > 0)) {
    throw new Error("Invalid frontend server configuration");
  }
  return createServer(async (request, response) => {
    const started = Date.now();
    const logPath = request.url?.split(/[?#]/, 1)[0] ?? "?";
    response.on("finish", () => {
      console.log(`[${role}] ${request.method} ${logPath} ${response.statusCode} ${Date.now() - started}ms`);
    });
    if (!(await allowedHost(request.headers.host, role, urlFile))) {
      sendJson(response, 400, { detail: "不允許的網址" });
      return;
    }
    const pathname = request.url?.split(/[?#]/, 1)[0];
    if (!pathname?.startsWith("/")) {
      sendJson(response, 400, { detail: "無效的網址" });
      return;
    }
    if (pathname.startsWith("/api/")) {
      proxyApi(request, response, apiPort);
      return;
    }
    if (pathname === "/health") {
      sendJson(response, 200, { status: "ok", role });
      return;
    }
    await serveStatic(request, response, pathname, distDir);
  });
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  const role = process.argv[2];
  const port = Number(process.argv[3]);
  const apiPort = Number(process.argv[4]);
  const server = createFrontendServer({ role, apiPort });
  server.listen(port, "127.0.0.1", () => {
    console.log(`TongLing ${role} frontend: http://127.0.0.1:${port} (API ${apiPort})`);
  });
  server.on("error", (error) => {
    console.error(error);
    process.exitCode = 1;
  });
}
