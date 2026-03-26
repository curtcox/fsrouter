#!/usr/bin/env python3
import json
import mimetypes
import os
import signal
import stat
import subprocess
import sys
import threading
import time
import urllib.parse
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Optional

HTTP_METHODS = {"GET", "HEAD", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"}


@dataclass
class Node:
    literal: dict[str, "Node"] = field(default_factory=dict)
    param: Optional["Node"] = None
    param_name: str = ""
    handlers: dict[str, Path] = field(default_factory=dict)
    implicit_handler: Optional[Path] = None

    def match(self, segs: list[str]) -> tuple[Optional["Node"], Optional[dict[str, str]]]:
        params: dict[str, str] = {}
        cur = self
        for seg in segs:
            if seg in cur.literal:
                cur = cur.literal[seg]
            elif cur.param is not None:
                params[cur.param.param_name] = seg
                cur = cur.param
            else:
                return None, None
        return cur, params


class FsrouterServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, server_address: tuple[str, int], handler_class, root: Node, timeout_seconds: int, listen_addr: str, route_dir_abs: Path):
        super().__init__(server_address, handler_class)
        self.root = root
        self.command_timeout = timeout_seconds
        self.listen_addr = listen_addr
        self.route_dir_abs = route_dir_abs


class Handler(BaseHTTPRequestHandler):
    server: FsrouterServer

    def do_GET(self):
        self.handle_method()

    def do_HEAD(self):
        self.handle_method()

    def do_POST(self):
        self.handle_method()

    def do_PUT(self):
        self.handle_method()

    def do_DELETE(self):
        self.handle_method()

    def do_PATCH(self):
        self.handle_method()

    def do_OPTIONS(self):
        self.handle_method()

    def handle_method(self):
        start = time.time()
        parsed = urllib.parse.urlsplit(self.path)

        try:
            segs = normalize_request_path(parsed.path)
        except ValueError:
            self.write_json(400, {"error": "invalid_path", "path": parsed.path})
            self.log_result(400, start)
            return

        node, params = self.server.root.match(segs)
        if node is None or (not node.handlers and node.implicit_handler is None):
            status = self.serve_filesystem_fallback(segs, parsed.path)
            self.log_result(status, start)
            return

        handler_path = node.handlers.get(self.command)
        if handler_path is not None:
            status = self.handle_handler(handler_path, params or {})
            self.log_result(status, start)
            return

        if node.implicit_handler is not None:
            status = self.handle_handler(node.implicit_handler, params or {})
            self.log_result(status, start)
            return

        allowed = sorted(node.handlers)
        body = json.dumps({"error": "method_not_allowed", "allow": allowed}, separators=(",", ":")).encode()
        self.send_response(405)
        self.send_header("Content-Type", "application/json")
        self.send_header("Allow", ", ".join(allowed))
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)
        self.log_result(405, start)

    def handle_handler(self, handler_path: Path, params: dict[str, str]) -> int:
        try:
            st = handler_path.stat()
        except OSError as err:
            self.write_json(500, {"error": "handler_stat_failed", "message": str(err)})
            return 500

        if not is_executable(st):
            return self.serve_static(handler_path)

        return self.execute_handler(handler_path, params)

    def serve_filesystem_fallback(self, segs: list[str], request_path: str) -> int:
        fallback = self.server.route_dir_abs.joinpath(*segs) if segs else self.server.route_dir_abs
        if fallback.is_file():
            return self.serve_fallback_file(fallback)
        if fallback.is_dir():
            preferred = self.find_directory_index(fallback)
            if preferred is not None:
                return self.serve_fallback_file(preferred)
            return self.serve_dir_listing(fallback, request_path)
        self.write_json(404, {"error": "not_found", "path": request_path})
        return 404

    def find_directory_index(self, dir_path: Path) -> Optional[Path]:
        for name in ("index.html", "index.htm"):
            path = dir_path / name
            if path.is_file():
                return path
        executable_indexes: list[Path] = []
        try:
            for entry in dir_path.iterdir():
                if not entry.is_file() or not entry.name.startswith("index."):
                    continue
                if is_executable(entry.stat()):
                    executable_indexes.append(entry)
        except OSError:
            return None
        if executable_indexes:
            return sorted(executable_indexes, key=lambda p: p.name)[0]
        return None

    def serve_fallback_file(self, path: Path) -> int:
        try:
            st = path.stat()
        except OSError as err:
            self.write_json(500, {"error": "handler_stat_failed", "message": str(err)})
            return 500
        if is_executable(st):
            return self.execute_handler(path, {})
        return self.serve_static(path)

    def execute_handler(self, path: Path, params: dict[str, str]) -> int:
        length = int(self.headers.get("Content-Length", "0") or "0")
        request_body = self.rfile.read(length) if length > 0 else b""
        env = build_env(self, params)

        try:
            proc = subprocess.Popen(
                [str(path)],
                cwd=str(path.parent),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=env,
            )
        except OSError as err:
            self.write_json(502, {"error": "exec_failed", "message": str(err)})
            return 502

        try:
            stdout, stderr = proc.communicate(request_body, timeout=self.server.command_timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.communicate()
            self.write_json(504, {"error": "handler_timeout", "timeout_seconds": self.server.command_timeout})
            return 504

        if stderr:
            sys.stderr.write(f"  [handler stderr] {stderr.decode('utf-8', errors='replace').rstrip()}\n")
            sys.stderr.flush()

        exit_code = proc.returncode or 0
        body = stdout
        if not body and exit_code != 0 and stderr:
            body = stderr

        status = exit_to_status(exit_code)
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)
        return status

    def serve_dir_listing(self, dir_path: Path, request_path: str) -> int:
        try:
            entries = sorted(dir_path.iterdir(), key=lambda e: (not e.is_dir(), e.name.lower()))
        except OSError as err:
            self.write_json(500, {"error": "dir_listing_failed", "message": str(err)})
            return 500
        title = f"Index of {request_path or '/'}"
        lines = [f"<!DOCTYPE html><html><head><title>{title}</title></head><body><h1>{title}</h1><ul>"]
        if request_path and request_path != "/":
            lines.append("<li><a href=\"../\">../</a></li>")
        for entry in entries:
            name = entry.name
            if entry.is_dir():
                lines.append(f"<li><a href=\"{name}/\">{name}/</a></li>")
            else:
                lines.append(f"<li><a href=\"{name}\">{name}</a></li>")
        lines.append("</ul></body></html>")
        body = "\n".join(lines).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)
        return 200

    def serve_static(self, handler_path: Path) -> int:
        try:
            data = handler_path.read_bytes()
        except OSError as err:
            self.write_json(500, {"error": "static_read_failed", "message": str(err)})
            return 500

        content_type = mimetypes.guess_type(str(handler_path))[0] or "application/octet-stream"
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)
        return 200

    def write_json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, separators=(",", ":")).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def log_message(self, fmt: str, *args):
        sys.stderr.write("%s - - [%s] %s\n" % (self.address_string(), self.log_date_time_string(), fmt % args))

    def log_result(self, status: int, start: float):
        elapsed = time.time() - start
        sys.stderr.write(f"{self.command} {urllib.parse.urlsplit(self.path).path} → {status} ({elapsed:.6f}s)\n")
        sys.stderr.flush()


def normalize_request_path(path: str) -> list[str]:
    segs: list[str] = []
    for segment in path.split("/"):
        if not segment:
            continue
        decoded = urllib.parse.unquote(segment)
        if decoded == "..":
            raise ValueError("invalid path")
        segs.append(decoded)
    return segs


def build_tree(route_dir: str) -> Node:
    root = Node()
    abs_dir = Path(route_dir).resolve(strict=True)
    for current_root, _, files in os.walk(abs_dir, followlinks=True):
        current_root_path = Path(current_root)
        for filename in files:
            file_path = current_root_path / filename
            method = filename.upper()
            if method in HTTP_METHODS:
                rel = file_path.parent.relative_to(abs_dir)
                segs = [segment for segment in rel.as_posix().split("/") if segment and segment != "."]
                cur = root
                for seg in segs:
                    if seg.startswith(":"):
                        if cur.param is None:
                            cur.param = Node(param_name=seg[1:])
                        cur = cur.param
                    else:
                        cur = cur.literal.setdefault(seg, Node())
                cur.handlers[method] = file_path
            else:
                try:
                    if not is_executable(file_path.stat()):
                        continue
                except OSError:
                    continue
                rel = file_path.relative_to(abs_dir)
                segs = [segment for segment in rel.as_posix().split("/") if segment and segment != "."]
                cur = root
                for seg in segs:
                    if seg.startswith(":"):
                        if cur.param is None:
                            cur.param = Node(param_name=seg[1:])
                        cur = cur.param
                    else:
                        cur = cur.literal.setdefault(seg, Node())
                cur.implicit_handler = file_path
    return root


def collect_routes(node: Node, prefix: str, items: list[tuple[str, str, Path, str]]) -> None:
    route = "/" if not prefix else f"/{prefix}"
    for method, path in node.handlers.items():
        try:
            tag = "exec" if is_executable(path.stat()) else "static"
        except OSError:
            tag = "unknown"
        items.append((route, method, path, tag))
    if node.implicit_handler is not None:
        items.append((route, "*", node.implicit_handler, "exec"))
    for seg in sorted(node.literal):
        collect_routes(node.literal[seg], join_prefix(prefix, seg), items)
    if node.param is not None:
        collect_routes(node.param, join_prefix(prefix, f":{node.param.param_name}"), items)


def join_prefix(prefix: str, seg: str) -> str:
    return seg if not prefix else f"{prefix}/{seg}"


def print_routes(root: Node, route_dir: str) -> None:
    sys.stderr.write(f"routes from {route_dir}:\n")
    items: list[tuple[str, str, Path, str]] = []
    collect_routes(root, "", items)
    items.sort(key=lambda item: (item[0], item[1]))
    for route, method, path, tag in items:
        sys.stderr.write(f"  {method:<7} {route:<45} → {path} [{tag}]\n")
    sys.stderr.flush()


def build_env(handler: Handler, params: dict[str, str]) -> dict[str, str]:
    parsed = urllib.parse.urlsplit(handler.path)
    env = dict(os.environ)
    env["REQUEST_METHOD"] = handler.command
    env["REQUEST_URI"] = handler.path
    env["REQUEST_PATH"] = parsed.path
    env["QUERY_STRING"] = parsed.query
    env["CONTENT_TYPE"] = handler.headers.get("Content-Type", "")
    env["CONTENT_LENGTH"] = handler.headers.get("Content-Length", "")
    env["REMOTE_ADDR"] = f"{handler.client_address[0]}:{handler.client_address[1]}"

    server_name, server_port = split_host_port(handler.headers.get("Host") or handler.server.listen_addr)
    env["SERVER_NAME"] = server_name
    if server_port:
        env["SERVER_PORT"] = server_port

    for key, value in params.items():
        env[f"PARAM_{env_key(key)}"] = value

    seen_query: set[str] = set()
    for key, value in urllib.parse.parse_qsl(parsed.query, keep_blank_values=True):
        if key not in seen_query:
            seen_query.add(key)
            env[f"QUERY_{env_key(key)}"] = value

    seen_headers: set[str] = set()
    for key in handler.headers:
        lower = key.lower()
        if lower in seen_headers:
            continue
        seen_headers.add(lower)
        env[f"HTTP_{key.upper().replace('-', '_')}"] = handler.headers.get(key, "")

    return env


def split_host_port(value: str) -> tuple[str, str]:
    if not value:
        return "", ""
    if value.startswith("[") and "]" in value:
        end = value.find("]")
        host = value[1:end]
        rest = value[end + 1 :]
        if rest.startswith(":"):
            return host, rest[1:]
        return host, ""
    if value.count(":") == 1:
        host, port = value.rsplit(":", 1)
        return host, port
    return value, ""


def env_key(value: str) -> str:
    return value.upper().replace("-", "_")


def exit_to_status(code: int) -> int:
    if code == 0:
        return 200
    if code == 1:
        return 400
    return 500


def is_executable(st: os.stat_result) -> bool:
    return bool(st.st_mode & (stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH))


def env_or(key: str, fallback: str) -> str:
    value = os.environ.get(key)
    return value if value else fallback


def parse_listen_addr(addr: str) -> tuple[str, int]:
    if addr.startswith(":"):
        return "", int(addr[1:])
    if addr.startswith("[") and "]" in addr:
        host, port = split_host_port(addr)
        return host, int(port)
    if addr.count(":") == 1:
        host, port = addr.rsplit(":", 1)
        return host, int(port)
    return addr, 8080


def main() -> int:
    route_dir = env_or("ROUTE_DIR", "./routes")
    listen_addr = env_or("LISTEN_ADDR", ":8080")
    try:
        timeout_seconds = int(env_or("COMMAND_TIMEOUT", "30"))
    except ValueError:
        timeout_seconds = 30
    if timeout_seconds <= 0:
        timeout_seconds = 30

    try:
        root = build_tree(route_dir)
    except OSError as err:
        sys.stderr.write(f"failed to scan {route_dir}: {err}\n")
        sys.stderr.flush()
        return 1

    print_routes(root, route_dir)
    host, port = parse_listen_addr(listen_addr)
    server = FsrouterServer((host, port), Handler, root, timeout_seconds, listen_addr, Path(route_dir).resolve())

    def shutdown_handler(signum, frame):
        sys.stderr.write("shutting down...\n")
        sys.stderr.flush()
        threading.Thread(target=server.shutdown, daemon=True).start()

    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, shutdown_handler)

    bound_host, bound_port = server.server_address[:2]
    display_addr = listen_addr if listen_addr else f"{bound_host}:{bound_port}"
    sys.stderr.write(f"listening on {display_addr} (timeout {timeout_seconds}s)\n")
    sys.stderr.flush()

    try:
        server.serve_forever()
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
