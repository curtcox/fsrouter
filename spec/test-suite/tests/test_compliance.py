import http.client
import json
import os
import socket
import subprocess
import tempfile
import time
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[3]
BASH_DIR = REPO_ROOT / "bash"
DENO_DIR = REPO_ROOT / "deno"
GROOVY_DIR = REPO_ROOT / "groovy"
GO_DIR = REPO_ROOT / "go"
JAVA_DIR = REPO_ROOT / "java"
LUA_DIR = REPO_ROOT / "lua"
PERL_DIR = REPO_ROOT / "perl"
RUST_DIR = REPO_ROOT / "rust"
PYTHON_DIR = REPO_ROOT / "python"
RUBY_DIR = REPO_ROOT / "ruby"


class RunningServer:
    def __init__(self, command: list[str], cwd: Path, route_dir: Path, timeout_seconds: int = 3, extra_env: dict[str, str] | None = None):
        self.command = command
        self.cwd = cwd
        self.route_dir = route_dir
        self.timeout_seconds = timeout_seconds
        self.extra_env = extra_env or {}
        self.port = self._free_port()
        self.base_url = f"127.0.0.1:{self.port}"
        self.process = None
        self.log_file = None

    def __enter__(self):
        env = os.environ.copy()
        env.update(self.extra_env)
        env["ROUTE_DIR"] = str(self.route_dir)
        env["LISTEN_ADDR"] = self.base_url
        env["COMMAND_TIMEOUT"] = str(self.timeout_seconds)
        self.log_file = tempfile.TemporaryFile(mode="w+t")
        self.process = subprocess.Popen(
            self.command,
            cwd=str(self.cwd),
            env=env,
            stdout=self.log_file,
            stderr=self.log_file,
            text=True,
        )
        deadline = time.time() + 5
        while time.time() < deadline:
            if self.process.poll() is not None:
                raise RuntimeError(f"server exited during startup\n{self.logs()}")
            try:
                with socket.create_connection(("127.0.0.1", self.port), timeout=0.2):
                    return self
            except OSError:
                time.sleep(0.05)
        self.stop()
        raise RuntimeError(f"server did not start listening\n{self.logs()}")

    def __exit__(self, exc_type, exc, tb):
        self.stop()

    def stop(self):
        if self.process is None:
            return
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=3)
        if self.log_file is not None:
            self.log_file.close()
        self.process = None
        self.log_file = None

    def logs(self) -> str:
        if self.log_file is None:
            return ""
        self.log_file.flush()
        self.log_file.seek(0)
        return self.log_file.read()

    def request(self, method: str, target: str, body: str | bytes | None = None, headers: dict[str, str] | None = None):
        payload = body.encode() if isinstance(body, str) else body
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            conn.request(method, target, body=payload, headers=headers or {})
            response = conn.getresponse()
            data = response.read()
            return response.status, response.headers, data
        finally:
            conn.close()

    @staticmethod
    def _free_port() -> int:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(("127.0.0.1", 0))
            return sock.getsockname()[1]


class FsrouterComplianceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        implementation = os.environ.get("FSROUTER_IMPL", "go").strip().lower()
        cls.implementation = implementation
        cls.build_dir = tempfile.TemporaryDirectory()
        binary_name = "fsrouter.exe" if os.name == "nt" else "fsrouter"

        if implementation == "bash":
            cls.binary = BASH_DIR / "fsrouter.sh"
            result = subprocess.run(
                ["bash", "-n", str(cls.binary)],
                cwd=str(BASH_DIR),
                capture_output=True,
                text=True,
            )
            cls.command = ["bash", str(cls.binary)]
            cls.command_cwd = BASH_DIR
        elif implementation == "deno":
            cls.binary = DENO_DIR / "fsrouter.ts"
            result = subprocess.run(
                ["deno", "check", str(cls.binary)],
                cwd=str(DENO_DIR),
                capture_output=True,
                text=True,
            )
            cls.command = ["deno", "run", "--allow-net", "--allow-read", "--allow-run", "--allow-env", str(cls.binary)]
            cls.command_cwd = DENO_DIR
        elif implementation == "groovy":
            cls.binary = GROOVY_DIR / "fsrouter.groovy"
            result = subprocess.run(
                ["groovyc", str(cls.binary)],
                cwd=str(GROOVY_DIR),
                capture_output=True,
                text=True,
            )
            cls.command = ["groovy", str(cls.binary)]
            cls.command_cwd = GROOVY_DIR
        elif implementation == "lua":
            cls.binary = LUA_DIR / "fsrouter.lua"
            result = subprocess.run(
                [
                    "lua",
                    "-e",
                    "assert(loadfile('fsrouter.lua')); assert(require('socket')); assert(require('system'))",
                ],
                cwd=str(LUA_DIR),
                capture_output=True,
                text=True,
            )
            cls.command = ["lua", str(cls.binary)]
            cls.command_cwd = LUA_DIR
        elif implementation == "perl":
            cls.binary = PERL_DIR / "fsrouter.pl"
            result = subprocess.run(
                ["perl", "-c", str(cls.binary)],
                cwd=str(PERL_DIR),
                capture_output=True,
                text=True,
            )
            cls.command = ["perl", str(cls.binary)]
            cls.command_cwd = PERL_DIR
        elif implementation == "go":
            cls.binary = Path(cls.build_dir.name) / binary_name
            result = subprocess.run(
                ["go", "build", "-o", str(cls.binary), "."],
                cwd=str(GO_DIR),
                capture_output=True,
                text=True,
            )
            cls.command = [str(cls.binary)]
            cls.command_cwd = cls.binary.parent
        elif implementation == "java":
            cls.binary = JAVA_DIR / "FSRouter.class"
            result = subprocess.run(
                ["javac", "FSRouter.java"],
                cwd=str(JAVA_DIR),
                capture_output=True,
                text=True,
            )
            cls.command = ["java", "FSRouter"]
            cls.command_cwd = JAVA_DIR
        elif implementation == "rust":
            cls.binary = RUST_DIR / "target" / "release" / binary_name
            result = subprocess.run(
                ["cargo", "build", "--release"],
                cwd=str(RUST_DIR),
                capture_output=True,
                text=True,
            )
            cls.command = [str(cls.binary)]
            cls.command_cwd = cls.binary.parent
        elif implementation == "python":
            cls.binary = PYTHON_DIR / "fsrouter.py"
            result = subprocess.run(
                ["python3", "-m", "py_compile", str(cls.binary)],
                cwd=str(PYTHON_DIR),
                capture_output=True,
                text=True,
            )
            cls.command = ["python3", str(cls.binary)]
            cls.command_cwd = PYTHON_DIR
        elif implementation == "ruby":
            cls.binary = RUBY_DIR / "fsrouter.rb"
            result = subprocess.run(
                ["ruby", "-c", str(cls.binary)],
                cwd=str(RUBY_DIR),
                capture_output=True,
                text=True,
            )
            cls.command = ["ruby", str(cls.binary)]
            cls.command_cwd = RUBY_DIR
        else:
            raise RuntimeError(f"unsupported FSROUTER_IMPL: {implementation}")

        if result.returncode != 0:
            raise RuntimeError(result.stderr or result.stdout)
        if not cls.binary.exists():
            raise RuntimeError(f"expected binary was not produced: {cls.binary}")

    @classmethod
    def tearDownClass(cls):
        cls.build_dir.cleanup()

    def make_routes(self) -> tempfile.TemporaryDirectory:
        return tempfile.TemporaryDirectory()

    def write_handler(self, routes: Path, relative_path: str, content: str, executable: bool = True) -> Path:
        path = routes / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        path.chmod(0o755 if executable else 0o644)
        return path

    def shell(self, body: str) -> str:
        return "#!/bin/sh\nset -e\n" + body.strip() + "\n"

    def parse_json(self, body: bytes):
        return json.loads(body.decode("utf-8"))

    def test_simple_get_returns_handler_stdout_as_json(self):
        with self.make_routes() as temp_dir:
            routes = Path(temp_dir)
            self.write_handler(routes, "hello/GET", self.shell("""printf '{\"message\":\"hello\"}'"""))
            with RunningServer(self.command, self.command_cwd, routes) as server:
                status, headers, body = server.request("GET", "/hello")
                self.assertEqual(status, 200)
                self.assertEqual(headers.get_content_type(), "application/json")
                self.assertEqual(self.parse_json(body), {"message": "hello"})

    def test_post_body_is_delivered_to_handler_stdin(self):
        with self.make_routes() as temp_dir:
            routes = Path(temp_dir)
            self.write_handler(routes, "echo/POST", self.shell("""cat"""))
            with RunningServer(self.command, self.command_cwd, routes) as server:
                payload = '{"name":"alice"}'
                status, _, body = server.request("POST", "/echo", body=payload, headers={"Content-Type": "application/json"})
                self.assertEqual(status, 200)
                self.assertEqual(body.decode("utf-8"), payload)

    def test_path_parameters_are_set_in_param_env_vars(self):
        with self.make_routes() as temp_dir:
            routes = Path(temp_dir)
            self.write_handler(routes, "users/:id/GET", self.shell('printf \'{"id":"%s"}\' "${PARAM_ID}"'))
            with RunningServer(self.command, self.command_cwd, routes) as server:
                status, _, body = server.request("GET", "/users/42")
                self.assertEqual(status, 200)
                self.assertEqual(self.parse_json(body), {"id": "42"})

    def test_query_parameters_are_set_in_query_env_vars(self):
        with self.make_routes() as temp_dir:
            routes = Path(temp_dir)
            self.write_handler(routes, "search/GET", self.shell('printf \'{"status":"%s"}\' "${QUERY_STATUS}"'))
            with RunningServer(self.command, self.command_cwd, routes) as server:
                status, _, body = server.request("GET", "/search?status=active")
                self.assertEqual(status, 200)
                self.assertEqual(self.parse_json(body), {"status": "active"})

    def test_literal_segments_take_priority_over_parameter_segments(self):
        with self.make_routes() as temp_dir:
            routes = Path(temp_dir)
            self.write_handler(routes, "users/me/GET", self.shell("""printf '{\"route\":\"literal\"}'"""))
            self.write_handler(routes, "users/:id/GET", self.shell('printf \'{"route":"param","id":"%s"}\' "${PARAM_ID}"'))
            with RunningServer(self.command, self.command_cwd, routes) as server:
                status_literal, _, body_literal = server.request("GET", "/users/me")
                status_param, _, body_param = server.request("GET", "/users/alice")
                self.assertEqual(status_literal, 200)
                self.assertEqual(status_param, 200)
                self.assertEqual(self.parse_json(body_literal), {"route": "literal"})
                self.assertEqual(self.parse_json(body_param), {"route": "param", "id": "alice"})

    def test_multiple_path_parameters_in_one_route_work(self):
        with self.make_routes() as temp_dir:
            routes = Path(temp_dir)
            self.write_handler(routes, "projects/:project_id/tasks/:task_id/GET", self.shell('printf \'{"project":"%s","task":"%s"}\' "${PARAM_PROJECT_ID}" "${PARAM_TASK_ID}"'))
            with RunningServer(self.command, self.command_cwd, routes) as server:
                status, _, body = server.request("GET", "/projects/acme/tasks/42")
                self.assertEqual(status, 200)
                self.assertEqual(self.parse_json(body), {"project": "acme", "task": "42"})

    def test_handler_can_set_status_code_via_status_header(self):
        with self.make_routes() as temp_dir:
            routes = Path(temp_dir)
            self.write_handler(routes, "widgets/POST", self.shell("""printf 'Status: 201\n\n{\"created\":true}'"""))
            with RunningServer(self.command, self.command_cwd, routes) as server:
                status, headers, body = server.request("POST", "/widgets")
                self.assertEqual(status, 201)
                self.assertEqual(headers.get_content_type(), "application/json")
                self.assertEqual(self.parse_json(body), {"created": True})

    def test_handler_can_set_custom_response_headers(self):
        with self.make_routes() as temp_dir:
            routes = Path(temp_dir)
            self.write_handler(routes, "headers/GET", self.shell("""printf 'X-Trace-Id: trace-123\n\n{\"ok\":true}'"""))
            with RunningServer(self.command, self.command_cwd, routes) as server:
                status, headers, body = server.request("GET", "/headers")
                self.assertEqual(status, 200)
                self.assertEqual(headers.get("X-Trace-Id"), "trace-123")
                self.assertEqual(self.parse_json(body), {"ok": True})

    def test_handler_can_override_content_type(self):
        with self.make_routes() as temp_dir:
            routes = Path(temp_dir)
            self.write_handler(routes, "plain/GET", self.shell("""printf 'Content-Type: text/plain\n\nhello world'"""))
            with RunningServer(self.command, self.command_cwd, routes) as server:
                status, headers, body = server.request("GET", "/plain")
                self.assertEqual(status, 200)
                self.assertEqual(headers.get_content_type(), "text/plain")
                self.assertEqual(body.decode("utf-8"), "hello world")

    def test_non_zero_exit_with_empty_stdout_uses_stderr_as_body(self):
        with self.make_routes() as temp_dir:
            routes = Path(temp_dir)
            self.write_handler(routes, "errors/GET", self.shell("""printf '{\"error\":\"bad_request\"}' >&2
exit 1"""))
            with RunningServer(self.command, self.command_cwd, routes) as server:
                status, headers, body = server.request("GET", "/errors")
                self.assertEqual(status, 400)
                self.assertEqual(headers.get_content_type(), "application/json")
                self.assertEqual(self.parse_json(body), {"error": "bad_request"})

    def test_handler_exceeding_timeout_returns_504(self):
        with self.make_routes() as temp_dir:
            routes = Path(temp_dir)
            self.write_handler(routes, "slow/GET", self.shell("""sleep 2
printf '{\"done\":true}'"""))
            with RunningServer(self.command, self.command_cwd, routes, timeout_seconds=1) as server:
                status, headers, body = server.request("GET", "/slow")
                self.assertEqual(status, 504)
                self.assertEqual(headers.get_content_type(), "application/json")
                self.assertEqual(self.parse_json(body), {"error": "handler_timeout", "timeout_seconds": 1})

    def test_valid_path_with_wrong_method_returns_405_with_allow_header(self):
        with self.make_routes() as temp_dir:
            routes = Path(temp_dir)
            self.write_handler(routes, "items/GET", self.shell("""printf '{\"method\":\"GET\"}'"""))
            self.write_handler(routes, "items/POST", self.shell("""printf '{\"method\":\"POST\"}'"""))
            with RunningServer(self.command, self.command_cwd, routes) as server:
                status, headers, body = server.request("DELETE", "/items")
                self.assertEqual(status, 405)
                self.assertEqual(headers.get("Allow"), "GET, POST")
                self.assertEqual(self.parse_json(body), {"error": "method_not_allowed", "allow": ["GET", "POST"]})

    def test_no_matching_path_returns_404(self):
        with self.make_routes() as temp_dir:
            routes = Path(temp_dir)
            self.write_handler(routes, "items/GET", self.shell("""printf '{\"ok\":true}'"""))
            with RunningServer(self.command, self.command_cwd, routes) as server:
                status, headers, body = server.request("GET", "/missing")
                self.assertEqual(status, 404)
                self.assertEqual(headers.get_content_type(), "application/json")
                self.assertEqual(self.parse_json(body), {"error": "not_found", "path": "/missing"})

    def test_non_executable_method_file_is_served_as_static_content(self):
        with self.make_routes() as temp_dir:
            routes = Path(temp_dir)
            self.write_handler(routes, "schema/GET", '{"schema":true}', executable=False)
            with RunningServer(self.command, self.command_cwd, routes) as server:
                status, _, body = server.request("GET", "/schema")
                self.assertEqual(status, 200)
                self.assertEqual(body.decode("utf-8"), '{"schema":true}')

    def test_all_seven_http_methods_are_dispatched_correctly(self):
        with self.make_routes() as temp_dir:
            routes = Path(temp_dir)
            for method in ["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"]:
                self.write_handler(routes, f"verbs/{method}", self.shell(f"printf '{{\"method\":\"{method}\"}}'"))
            self.write_handler(routes, "verbs/HEAD", self.shell("""printf 'X-Method: HEAD\n\nhead-body'"""))
            with RunningServer(self.command, self.command_cwd, routes) as server:
                for method in ["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"]:
                    status, _, body = server.request(method, "/verbs")
                    self.assertEqual(status, 200)
                    self.assertEqual(self.parse_json(body), {"method": method})
                status, headers, body = server.request("HEAD", "/verbs")
                self.assertEqual(status, 200)
                self.assertEqual(headers.get("X-Method"), "HEAD")
                self.assertEqual(body, b"")

    def test_request_headers_are_available_as_http_env_vars(self):
        with self.make_routes() as temp_dir:
            routes = Path(temp_dir)
            self.write_handler(routes, "headers/GET", self.shell('printf \'{"request_id":"%s","accept":"%s"}\' "${HTTP_X_REQUEST_ID}" "${HTTP_ACCEPT}"'))
            with RunningServer(self.command, self.command_cwd, routes) as server:
                status, _, body = server.request("GET", "/headers", headers={"X-Request-Id": "req-123", "Accept": "application/json"})
                self.assertEqual(status, 200)
                self.assertEqual(self.parse_json(body), {"request_id": "req-123", "accept": "application/json"})

    def test_server_environment_is_inherited_by_handlers(self):
        with self.make_routes() as temp_dir:
            routes = Path(temp_dir)
            self.write_handler(routes, "env/GET", self.shell('printf \'{"token":"%s"}\' "${FSROUTER_SUITE_TOKEN}"'))
            with RunningServer(self.command, self.command_cwd, routes, extra_env={"FSROUTER_SUITE_TOKEN": "secret-token"}) as server:
                status, _, body = server.request("GET", "/env")
                self.assertEqual(status, 200)
                self.assertEqual(self.parse_json(body), {"token": "secret-token"})

    def test_handler_cwd_is_set_to_handlers_parent_directory(self):
        with self.make_routes() as temp_dir:
            routes = Path(temp_dir)
            reports_dir = routes / "reports"
            reports_dir.mkdir(parents=True, exist_ok=True)
            (reports_dir / "value.txt").write_text("from-sibling", encoding="utf-8")
            self.write_handler(routes, "reports/GET", self.shell("""cat value.txt"""))
            with RunningServer(self.command, self.command_cwd, routes) as server:
                status, _, body = server.request("GET", "/reports")
                self.assertEqual(status, 200)
                self.assertEqual(body.decode("utf-8"), "from-sibling")

    def test_trailing_slashes_are_normalized(self):
        with self.make_routes() as temp_dir:
            routes = Path(temp_dir)
            self.write_handler(routes, "hello/GET", self.shell("""printf '{\"message\":\"hello\"}'"""))
            with RunningServer(self.command, self.command_cwd, routes) as server:
                status, _, body = server.request("GET", "/hello/")
                self.assertEqual(status, 200)
                self.assertEqual(self.parse_json(body), {"message": "hello"})

    def test_hyphens_in_param_and_query_names_become_underscores(self):
        with self.make_routes() as temp_dir:
            routes = Path(temp_dir)
            self.write_handler(routes, "runs/:run-id/GET", self.shell('printf \'{"run_id":"%s","per_page":"%s"}\' "${PARAM_RUN_ID}" "${QUERY_PER_PAGE}"'))
            with RunningServer(self.command, self.command_cwd, routes) as server:
                status, _, body = server.request("GET", "/runs/run-001?per-page=20")
                self.assertEqual(status, 200)
                self.assertEqual(self.parse_json(body), {"run_id": "run-001", "per_page": "20"})

    def test_arbitrary_file_in_route_dir_is_served_directly(self):
        with self.make_routes() as temp_dir:
            routes = Path(temp_dir)
            notes = routes / "notes.txt"
            notes.write_text("hello from notes", encoding="utf-8")
            with RunningServer(self.command, self.command_cwd, routes) as server:
                status, headers, body = server.request("GET", "/notes.txt")
                self.assertEqual(status, 200)
                self.assertIn("text/plain", headers.get_content_type())
                self.assertEqual(body.decode("utf-8"), "hello from notes")

    def test_directory_in_route_dir_returns_html_listing(self):
        with self.make_routes() as temp_dir:
            routes = Path(temp_dir)
            docs = routes / "docs"
            docs.mkdir()
            (docs / "readme.txt").write_text("doc content", encoding="utf-8")
            with RunningServer(self.command, self.command_cwd, routes) as server:
                status, headers, body = server.request("GET", "/docs")
                self.assertEqual(status, 200)
                self.assertIn("text/html", headers.get_content_type())
                self.assertIn(b"readme.txt", body)

    def test_directory_prefers_index_html_over_listing(self):
        with self.make_routes() as temp_dir:
            routes = Path(temp_dir)
            docs = routes / "docs"
            docs.mkdir()
            (docs / "index.html").write_text("<h1>docs home</h1>", encoding="utf-8")
            (docs / "readme.txt").write_text("doc content", encoding="utf-8")
            with RunningServer(self.command, self.command_cwd, routes) as server:
                status, headers, body = server.request("GET", "/docs")
                self.assertEqual(status, 200)
                self.assertIn("text/html", headers.get_content_type())
                self.assertEqual(body.decode("utf-8"), "<h1>docs home</h1>")

    def test_directory_prefers_html_index_over_executable_index(self):
        with self.make_routes() as temp_dir:
            routes = Path(temp_dir)
            docs = routes / "docs"
            docs.mkdir()
            (docs / "index.html").write_text("<h1>static index</h1>", encoding="utf-8")
            self.write_handler(routes, "docs/index.sh", self.shell("""printf '{"source":"exec"}'"""))
            with RunningServer(self.command, self.command_cwd, routes) as server:
                status, headers, body = server.request("GET", "/docs")
                self.assertEqual(status, 200)
                self.assertIn("text/html", headers.get_content_type())
                self.assertEqual(body.decode("utf-8"), "<h1>static index</h1>")

    def test_directory_executes_executable_index_file(self):
        with self.make_routes() as temp_dir:
            routes = Path(temp_dir)
            docs = routes / "docs"
            docs.mkdir()
            self.write_handler(routes, "docs/index.sh", self.shell("""printf '{"source":"exec"}'"""))
            with RunningServer(self.command, self.command_cwd, routes) as server:
                status, headers, body = server.request("GET", "/docs")
                self.assertEqual(status, 200)
                self.assertEqual(headers.get_content_type(), "application/json")
                self.assertEqual(self.parse_json(body), {"source": "exec"})

    def test_directory_executable_index_runs_in_its_directory(self):
        with self.make_routes() as temp_dir:
            routes = Path(temp_dir)
            docs = routes / "docs"
            docs.mkdir()
            (docs / "value.txt").write_text("from-index-dir", encoding="utf-8")
            self.write_handler(routes, "docs/index.sh", self.shell("""cat value.txt"""))
            with RunningServer(self.command, self.command_cwd, routes) as server:
                status, _, body = server.request("GET", "/docs")
                self.assertEqual(status, 200)
                self.assertEqual(body.decode("utf-8"), "from-index-dir")


if __name__ == "__main__":
    unittest.main(verbosity=2)
