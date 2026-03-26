const HTTP_METHODS = new Set(["GET", "HEAD", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"]);

type MatchResult = {
  node: Node | null;
  params: Record<string, string> | null;
};

class Node {
  literal = new Map<string, Node>();
  param: Node | null = null;
  paramName = "";
  handlers = new Map<string, string>();
  implicitHandler: string | null = null;

  match(segs: string[]): MatchResult {
    const params: Record<string, string> = {};
    let cur: Node = this;
    for (const seg of segs) {
      const literal = cur.literal.get(seg);
      if (literal) {
        cur = literal;
        continue;
      }
      if (cur.param) {
        params[cur.param.paramName] = seg;
        cur = cur.param;
        continue;
      }
      return { node: null, params: null };
    }
    return { node: cur, params };
  }
}

type RouteItem = {
  route: string;
  method: string;
  path: string;
  tag: string;
};

const textDecoder = new TextDecoder();
const textEncoder = new TextEncoder();

function envOr(key: string, fallback: string): string {
  const value = Deno.env.get(key);
  return value && value.length > 0 ? value : fallback;
}

function parseTimeout(value: string): number {
  const parsed = Number.parseInt(value, 10);
  return Number.isFinite(parsed) && parsed > 0 ? parsed : 30;
}

function normalizeRequestPath(path: string): string[] {
  let collapsed = path.replace(/\/+/g, "/");
  if (!collapsed) {
    collapsed = "/";
  }
  const trimmed = collapsed === "/" ? collapsed : collapsed.replace(/\/$/, "");
  const segs: string[] = [];
  for (const segment of trimmed.split("/")) {
    if (!segment) {
      continue;
    }
    const decoded = decodeURIComponent(segment);
    if (decoded === "..") {
      throw new Error("invalid path");
    }
    segs.push(decoded);
  }
  return segs;
}

function joinFsPath(base: string, part: string): string {
  if (base.endsWith("/")) {
    return `${base}${part}`;
  }
  return `${base}/${part}`;
}

function joinRoute(prefix: string, seg: string): string {
  return prefix ? `${prefix}/${seg}` : seg;
}

async function buildTree(routeDir: string): Promise<Node> {
  const absDir = await Deno.realPath(routeDir);
  const root = new Node();
  await walkRouteDir(absDir, absDir, root);
  return root;
}

async function walkRouteDir(baseDir: string, currentDir: string, root: Node): Promise<void> {
  for await (const entry of Deno.readDir(currentDir)) {
    const fullPath = joinFsPath(currentDir, entry.name);
    if (entry.isDirectory) {
      await walkRouteDir(baseDir, fullPath, root);
      continue;
    }
    if (!entry.isFile) {
      continue;
    }
    const method = entry.name.toUpperCase();
    if (HTTP_METHODS.has(method)) {
      const parent = currentDir;
      const relativeParent = parent === baseDir ? "" : parent.slice(baseDir.length + 1);
      let cur = root;
      if (relativeParent) {
        for (const seg of relativeParent.split("/")) {
          if (seg.startsWith(":")) {
            if (!cur.param) {
              cur.param = new Node();
              cur.param.paramName = seg.slice(1);
            }
            cur = cur.param;
          } else {
            let next = cur.literal.get(seg);
            if (!next) {
              next = new Node();
              cur.literal.set(seg, next);
            }
            cur = next;
          }
        }
      }
      cur.handlers.set(method, fullPath);
    } else if (await isExecutable(fullPath).catch(() => false)) {
      const relativePath = fullPath === baseDir ? "" : fullPath.slice(baseDir.length + 1);
      let cur = root;
      if (relativePath) {
        for (const seg of relativePath.split("/")) {
          if (seg.startsWith(":")) {
            if (!cur.param) {
              cur.param = new Node();
              cur.param.paramName = seg.slice(1);
            }
            cur = cur.param;
          } else {
            let next = cur.literal.get(seg);
            if (!next) {
              next = new Node();
              cur.literal.set(seg, next);
            }
            cur = next;
          }
        }
      }
      cur.implicitHandler = fullPath;
    }
  }
}

async function isExecutable(path: string): Promise<boolean> {
  const info = await Deno.stat(path);
  if (info.mode != null) {
    return (info.mode & 0o111) !== 0;
  }
  return true;
}

async function collectRoutes(node: Node, prefix: string, items: RouteItem[]): Promise<void> {
  const route = prefix ? `/${prefix}` : "/";
  for (const [method, path] of [...node.handlers.entries()].sort(([a], [b]) => a.localeCompare(b))) {
    let tag = "unknown";
    try {
      tag = (await isExecutable(path)) ? "exec" : "static";
    } catch {
      tag = "unknown";
    }
    items.push({ route, method, path, tag });
  }
  if (node.implicitHandler !== null) {
    items.push({ route, method: "*", path: node.implicitHandler, tag: "exec" });
  }
  for (const seg of [...node.literal.keys()].sort()) {
    await collectRoutes(node.literal.get(seg)!, joinRoute(prefix, seg), items);
  }
  if (node.param) {
    await collectRoutes(node.param, joinRoute(prefix, `:${node.param.paramName}`), items);
  }
}

async function printRoutes(root: Node, routeDir: string): Promise<void> {
  console.error(`routes from ${routeDir}:`);
  const items: RouteItem[] = [];
  await collectRoutes(root, "", items);
  items.sort((a, b) => a.route.localeCompare(b.route) || a.method.localeCompare(b.method));
  for (const item of items) {
    console.error(`  ${item.method.padEnd(7)} ${item.route.padEnd(45)} → ${item.path} [${item.tag}]`);
  }
}

function splitHostPort(value: string): { host: string; port: string } {
  if (!value) {
    return { host: "", port: "" };
  }
  if (value.startsWith("[") && value.includes("]")) {
    const end = value.indexOf("]");
    const host = value.slice(1, end);
    const rest = value.slice(end + 1);
    if (rest.startsWith(":")) {
      return { host, port: rest.slice(1) };
    }
    return { host, port: "" };
  }
  const colonCount = [...value].filter((ch) => ch === ":").length;
  if (colonCount === 1) {
    const idx = value.lastIndexOf(":");
    return { host: value.slice(0, idx), port: value.slice(idx + 1) };
  }
  return { host: value, port: "" };
}

function envKey(value: string): string {
  return value.toUpperCase().replace(/-/g, "_");
}

function buildEnv(request: Request, params: Record<string, string>, remoteAddr: Deno.NetAddr | null, listenAddr: string): Record<string, string> {
  const env: Record<string, string> = { ...Deno.env.toObject() };
  const url = new URL(request.url);
  env.REQUEST_METHOD = request.method;
  env.REQUEST_URI = url.pathname + url.search;
  env.REQUEST_PATH = url.pathname;
  env.QUERY_STRING = url.search.startsWith("?") ? url.search.slice(1) : "";
  env.CONTENT_TYPE = request.headers.get("content-type") ?? "";
  env.CONTENT_LENGTH = request.headers.get("content-length") ?? "";
  env.REMOTE_ADDR = remoteAddr ? `${remoteAddr.hostname}:${remoteAddr.port}` : "";

  const hostHeader = request.headers.get("host") ?? listenAddr;
  const hostPort = splitHostPort(hostHeader);
  env.SERVER_NAME = hostPort.host;
  if (hostPort.port) {
    env.SERVER_PORT = hostPort.port;
  }

  for (const [key, value] of Object.entries(params)) {
    env[`PARAM_${envKey(key)}`] = value;
  }

  const seenQuery = new Set<string>();
  for (const [key, value] of url.searchParams.entries()) {
    if (seenQuery.has(key)) {
      continue;
    }
    seenQuery.add(key);
    env[`QUERY_${envKey(key)}`] = value;
  }

  const seenHeaders = new Set<string>();
  for (const [key, value] of request.headers.entries()) {
    const lower = key.toLowerCase();
    if (seenHeaders.has(lower)) {
      continue;
    }
    seenHeaders.add(lower);
    env[`HTTP_${key.toUpperCase().replace(/-/g, "_")}`] = value;
  }

  return env;
}

function exitToStatus(code: number): number {
  if (code === 0) {
    return 200;
  }
  if (code === 1) {
    return 400;
  }
  return 500;
}

function responseBody(body: Uint8Array): ArrayBuffer {
  const copy = new Uint8Array(body.byteLength);
  copy.set(body);
  return copy.buffer;
}

function jsonResponse(status: number, payload: unknown, method: string, headers: HeadersInit = {}): Response {
  const bodyText = JSON.stringify(payload);
  const initHeaders = new Headers(headers);
  initHeaders.set("content-type", "application/json");
  initHeaders.set("content-length", String(textEncoder.encode(bodyText).length));
  return new Response(method === "HEAD" ? null : bodyText, { status, headers: initHeaders });
}

async function executeHandler(handlerPath: string, request: Request, params: Record<string, string>, timeoutSeconds: number, remoteAddr: Deno.NetAddr | null, listenAddr: string): Promise<Response> {
  const requestBody = new Uint8Array(await request.arrayBuffer());
  const env = buildEnv(request, params, remoteAddr, listenAddr);
  const cwd = handlerPath.includes("/") ? handlerPath.slice(0, handlerPath.lastIndexOf("/")) : ".";

  let child: Deno.ChildProcess;
  try {
    child = new Deno.Command(handlerPath, {
      cwd,
      env,
      stdin: "piped",
      stdout: "piped",
      stderr: "piped",
    }).spawn();
  } catch (error) {
    return jsonResponse(502, { error: "exec_failed", message: error instanceof Error ? error.message : String(error) }, request.method);
  }

  const stdinPromise = (async () => {
    if (child.stdin) {
      const writer = child.stdin.getWriter();
      await writer.write(requestBody);
      await writer.close();
    }
  })();

  const stdoutPromise = child.stdout ? new Response(child.stdout).arrayBuffer().then((buf) => new Uint8Array(buf)) : Promise.resolve(new Uint8Array());
  const stderrPromise = child.stderr ? new Response(child.stderr).arrayBuffer().then((buf) => new Uint8Array(buf)) : Promise.resolve(new Uint8Array());

  let timedOut = false;
  const timeoutPromise = new Promise<never>((_, reject) => {
    const id = setTimeout(() => {
      timedOut = true;
      try {
        child.kill("SIGKILL");
      } catch {
      }
      clearTimeout(id);
      reject(new Error("timeout"));
    }, timeoutSeconds * 1000);
  });

  let status: Deno.CommandStatus;
  let stdout: Uint8Array;
  let stderr: Uint8Array;
  try {
    await stdinPromise;
    status = await Promise.race([child.status, timeoutPromise]);
    [stdout, stderr] = await Promise.all([stdoutPromise, stderrPromise]);
  } catch (error) {
    if (timedOut) {
      return jsonResponse(504, { error: "handler_timeout", timeout_seconds: timeoutSeconds }, request.method);
    }
    return jsonResponse(500, { error: "exec_failed", message: error instanceof Error ? error.message : String(error) }, request.method);
  }

  if (stderr.length > 0) {
    console.error(`  [handler stderr] ${textDecoder.decode(stderr).replace(/\n$/, "")}`);
  }

  const body = (stdout.length === 0 && status.code !== 0 && stderr.length > 0) ? stderr : stdout;
  const responseStatus = exitToStatus(status.code);
  const headers = new Headers();
  headers.set("content-type", "application/json");
  headers.set("content-length", String(body.length));
  return new Response(request.method === "HEAD" ? null : responseBody(body), { status: responseStatus, headers });
}

const MIME_TYPES: Record<string, string> = {
  ".html": "text/html", ".htm": "text/html",
  ".css": "text/css", ".js": "application/javascript",
  ".json": "application/json", ".xml": "application/xml",
  ".txt": "text/plain", ".md": "text/plain",
  ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
  ".gif": "image/gif", ".svg": "image/svg+xml", ".ico": "image/x-icon",
  ".pdf": "application/pdf", ".zip": "application/zip",
  ".sh": "text/plain", ".py": "text/plain", ".rb": "text/plain",
};

function mimeTypeFor(path: string): string {
  const dot = path.lastIndexOf(".");
  if (dot === -1) return "application/octet-stream";
  return MIME_TYPES[path.slice(dot).toLowerCase()] ?? "application/octet-stream";
}

async function serveStatic(handlerPath: string, request: Request): Promise<Response> {
  try {
    const body = await Deno.readFile(handlerPath);
    const headers = new Headers();
    headers.set("content-type", mimeTypeFor(handlerPath));
    headers.set("content-length", String(body.length));
    return new Response(request.method === "HEAD" ? null : responseBody(body), { status: 200, headers });
  } catch (error) {
    return jsonResponse(500, { error: "static_read_failed", message: error instanceof Error ? error.message : String(error) }, request.method);
  }
}

function parseListenAddr(addr: string): { hostname: string; port: number } {
  if (addr.startsWith(":")) {
    return { hostname: "0.0.0.0", port: Number.parseInt(addr.slice(1), 10) };
  }
  if (addr.startsWith("[") && addr.includes("]")) {
    const { host, port } = splitHostPort(addr);
    return { hostname: host, port: Number.parseInt(port, 10) };
  }
  if ([...addr].filter((ch) => ch === ":").length === 1) {
    const idx = addr.lastIndexOf(":");
    return { hostname: addr.slice(0, idx), port: Number.parseInt(addr.slice(idx + 1), 10) };
  }
  return { hostname: addr, port: 8080 };
}

function logResult(request: Request, status: number, start: number): void {
  const url = new URL(request.url);
  const elapsed = (performance.now() - start) / 1000;
  console.error(`${request.method} ${url.pathname} → ${status} (${elapsed.toFixed(6)}s)`);
}

async function serveFilesystem(
  routeDir: string,
  segs: string[],
  requestPath: string,
  request: Request,
  timeoutSeconds: number,
  listenAddr: string,
  remoteAddr: Deno.NetAddr | null,
): Promise<Response> {
  const fullPath = segs.length === 0 ? routeDir : [routeDir, ...segs].join("/");
  let info: Deno.FileInfo;
  try {
    info = await Deno.stat(fullPath);
  } catch {
    return jsonResponse(404, { error: "not_found", path: requestPath }, request.method);
  }
  if (!info.isDirectory) {
    return serveFallbackFile(fullPath, info, request, timeoutSeconds, listenAddr, remoteAddr);
  }
  const preferred = await findDirectoryIndex(fullPath);
  if (preferred) {
    return serveFallbackFile(preferred.path, null, request, timeoutSeconds, listenAddr, remoteAddr);
  }
  return serveDirListing(fullPath, requestPath, request.method);
}

async function serveFallbackFile(
  path: string,
  info: Deno.FileInfo | null,
  request: Request,
  timeoutSeconds: number,
  listenAddr: string,
  remoteAddr: Deno.NetAddr | null,
): Promise<Response> {
  const fileInfo = info ?? await Deno.stat(path);
  if (fileInfo.mode != null && (fileInfo.mode & 0o111) !== 0) {
    return executeHandler(path, request, {}, timeoutSeconds, remoteAddr, listenAddr);
  }
  return serveStatic(path, request);
}

async function findDirectoryIndex(dirPath: string): Promise<{ kind: "static" | "exec"; path: string } | null> {
  for (const name of ["index.html", "index.htm"]) {
    const candidate = joinFsPath(dirPath, name);
    try {
      const info = await Deno.stat(candidate);
      if (info.isFile) {
        return { kind: "static", path: candidate };
      }
    } catch {
    }
  }

  const executableIndexes: string[] = [];
  try {
    for await (const entry of Deno.readDir(dirPath)) {
      if (!entry.isFile || !entry.name.startsWith("index.")) {
        continue;
      }
      const candidate = joinFsPath(dirPath, entry.name);
      if (await isExecutable(candidate)) {
        executableIndexes.push(candidate);
      }
    }
  } catch {
    return null;
  }
  executableIndexes.sort((a, b) => a.localeCompare(b));
  return executableIndexes.length > 0 ? { kind: "exec", path: executableIndexes[0] } : null;
}

async function serveDirListing(dirPath: string, requestPath: string, method: string): Promise<Response> {
  const entries: Array<{ name: string; isDir: boolean }> = [];
  try {
    for await (const entry of Deno.readDir(dirPath)) {
      entries.push({ name: entry.name, isDir: entry.isDirectory });
    }
  } catch (error) {
    return jsonResponse(500, { error: "dir_listing_failed", message: error instanceof Error ? error.message : String(error) }, method);
  }
  entries.sort((a, b) => a.name.localeCompare(b.name));
  const title = `Index of ${requestPath}`;
  let html = `<!DOCTYPE html><html><head><title>${title}</title></head><body><h1>${title}</h1><ul>`;
  if (requestPath !== "/") {
    html += `<li><a href="../">../</a></li>`;
  }
  for (const entry of entries) {
    if (entry.isDir) {
      html += `<li><a href="${entry.name}/">${entry.name}/</a></li>`;
    } else {
      html += `<li><a href="${entry.name}">${entry.name}</a></li>`;
    }
  }
  html += "</ul></body></html>";
  const body = textEncoder.encode(html);
  const headers = new Headers();
  headers.set("content-type", "text/html; charset=utf-8");
  headers.set("content-length", String(body.length));
  return new Response(method === "HEAD" ? null : responseBody(body), { status: 200, headers });
}

async function handleRequest(request: Request, root: Node, timeoutSeconds: number, listenAddr: string, remoteAddr: Deno.NetAddr | null, routeDir: string): Promise<Response> {
  const start = performance.now();
  const url = new URL(request.url);
  let response: Response;

  try {
    const segs = normalizeRequestPath(url.pathname);
    const match = root.match(segs);
    if (!match.node || (match.node.handlers.size === 0 && match.node.implicitHandler === null)) {
      response = await serveFilesystem(routeDir, segs, url.pathname, request, timeoutSeconds, listenAddr, remoteAddr);
      logResult(request, response.status, start);
      return response;
    }

    let handlerPath = match.node.handlers.get(request.method) ?? null;
    if (!handlerPath && request.method === "HEAD") {
      handlerPath = match.node.handlers.get("GET") ?? null;
    }

    if (!handlerPath && match.node.implicitHandler !== null) {
      handlerPath = match.node.implicitHandler;
    }

    if (!handlerPath) {
      if (match.node.handlers.size > 0) {
        const allowed = [...match.node.handlers.keys()].sort();
        response = jsonResponse(405, { error: "method_not_allowed", allow: allowed }, request.method, {
          allow: allowed.join(", "),
        });
        logResult(request, response.status, start);
        return response;
      } else {
        response = await serveFilesystem(routeDir, segs, url.pathname, request, timeoutSeconds, listenAddr, remoteAddr);
        logResult(request, response.status, start);
        return response;
      }
    }

    try {
      if (await isExecutable(handlerPath)) {
        response = await executeHandler(handlerPath, request, match.params ?? {}, timeoutSeconds, remoteAddr, listenAddr);
      } else {
        response = await serveStatic(handlerPath, request);
      }
    } catch (error) {
      response = jsonResponse(500, { error: "handler_stat_failed", message: error instanceof Error ? error.message : String(error) }, request.method);
    }
  } catch {
    response = jsonResponse(400, { error: "invalid_path", path: url.pathname }, request.method);
  }

  logResult(request, response.status, start);
  return response;
}

async function main(): Promise<void> {
  const routeDir = envOr("ROUTE_DIR", "./routes");
  const listenAddr = envOr("LISTEN_ADDR", ":8080");
  const timeoutSeconds = parseTimeout(envOr("COMMAND_TIMEOUT", "30"));

  let root: Node;
  try {
    root = await buildTree(routeDir);
  } catch (error) {
    console.error(`failed to scan ${routeDir}: ${error instanceof Error ? error.message : String(error)}`);
    Deno.exit(1);
    throw error;
  }

  await printRoutes(root, routeDir);
  const bind = parseListenAddr(listenAddr);
  const controller = new AbortController();

  const shutdown = () => {
    console.error("shutting down...");
    controller.abort();
  };
  Deno.addSignalListener("SIGINT", shutdown);
  Deno.addSignalListener("SIGTERM", shutdown);

  console.error(`listening on ${listenAddr} (timeout ${timeoutSeconds}s)`);
  const absRouteDir = await Deno.realPath(routeDir).catch(() => routeDir);
  const server = Deno.serve({ hostname: bind.hostname, port: bind.port, signal: controller.signal }, (request: Request, info: Deno.ServeHandlerInfo<Deno.NetAddr>) => {
    const remoteAddr = info.remoteAddr?.transport === "tcp" ? info.remoteAddr : null;
    return handleRequest(request, root, timeoutSeconds, listenAddr, remoteAddr, absRouteDir);
  });
  await server.finished;
}

if (import.meta.main) {
  await main();
}
