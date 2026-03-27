import json
import os
import urllib.request
import unittest

from examples.ai.agent_loop import BudgetRef
from examples.ai.tests.test_support import AgentLoopBuildTestCase

@unittest.skipUnless(os.environ.get("OPENROUTER_API_KEY"), "OPENROUTER_API_KEY not set")
class AgentLoopBuildIntegrationTests(AgentLoopBuildTestCase):
    def test_qr_code_reader_integration(self):
            result = self._run_build(
                (
                    "Create a web app with a page that opens the device camera, detects QR codes in the "
                    "video feed, and displays the decoded contents. When a QR code is detected, present "
                    "actions that fit the content and always include Say it and Use as change prompt."
                ),
                "qr-code-reader",
            )
            result_type = result.get("type") if isinstance(result, dict) else None
            self.assertNotIn(
                result_type,
                {"error", "budget_exhausted", "user_aborted"},
                self._build_debug_context("qr-code-reader", result),
            )
            self.assertTrue(any(self.routes_dir.rglob("*")))
            commands = json.loads((self.log_dir / "qr-code-reader" / "commands.json").read_text(encoding="utf-8"))
            self.assertTrue(any("routes" in entry["command"] or "find" in entry["command"] or "ls" in entry["command"] for entry in commands))
            server, base_url = self._start_server()
            try:
                html = urllib.request.urlopen(base_url + "/").read().decode("utf-8", errors="replace")
            finally:
                self._stop_server(server)
            route_files = sorted(str(path.relative_to(self.routes_dir)) for path in self.routes_dir.rglob("*") if path.is_file())
            self.assertTrue(
                "<video" in html or "getUserMedia" in html,
                f"Expected camera UI in root HTML.\nRoute files: {route_files}\nHTML (first 2000 chars):\n{html[:2000]}",
            )
            self.assertTrue(
                "jsQR" in html or "zxing" in html or "qr" in html.lower(),
                f"Expected QR logic in root HTML.\nRoute files: {route_files}\nHTML (first 2000 chars):\n{html[:2000]}",
            )
            self.assertTrue(
                "Say it" in html or "say it" in html.lower(),
                f"Expected spoken-action UI in root HTML.\nRoute files: {route_files}\nHTML (first 2000 chars):\n{html[:2000]}",
            )
            self.assertIn("change", html.lower())

    def test_network_scanner_integration(self):
            result = self._run_build(
                (
                    "Create a web app that scans the local network on the server side, shows progress, "
                    "renders results in a table, and includes a topology graph."
                ),
                "network-scanner",
            )
            result_type = result.get("type") if isinstance(result, dict) else None
            self.assertNotIn(
                result_type,
                {"error", "budget_exhausted", "user_aborted"},
                self._build_debug_context("network-scanner", result),
            )
            commands = json.loads((self.log_dir / "network-scanner" / "commands.json").read_text(encoding="utf-8"))
            self.assertTrue(any(("which nmap" in entry["command"]) or ("which arp" in entry["command"]) or ("command -v" in entry["command"]) for entry in commands))
            server, base_url = self._start_server()
            try:
                html = urllib.request.urlopen(base_url + "/").read().decode("utf-8", errors="replace")
            finally:
                self._stop_server(server)
            route_files = sorted(str(path.relative_to(self.routes_dir)) for path in self.routes_dir.rglob("*") if path.is_file())
            self.assertTrue(
                "<table" in html or "services" in html.lower(),
                f"Expected table/services UI in root HTML.\nRoute files: {route_files}\nHTML (first 2000 chars):\n{html[:2000]}",
            )
            self.assertTrue(
                "svg" in html.lower() or "canvas" in html.lower() or "graph" in html.lower(),
                f"Expected graph/topology UI in root HTML.\nRoute files: {route_files}\nHTML (first 2000 chars):\n{html[:2000]}",
            )
            self.assertTrue(
                "setInterval" in html or "setTimeout" in html or "fetch(" in html,
                f"Expected polling logic in root HTML.\nRoute files: {route_files}\nHTML (first 2000 chars):\n{html[:2000]}",
            )

    def test_scheduler_frontend_integration(self):
            result = self._run_build(
                (
                    "Create a web app frontend for cron and launchd task management on macOS, including "
                    "listing tasks, adding cron jobs, removing cron jobs, enabling and disabling launchd "
                    "agents, and confirming destructive actions."
                ),
                "scheduler",
            )
            result_type = result.get("type") if isinstance(result, dict) else None
            self.assertNotIn(
                result_type,
                {"error", "budget_exhausted", "user_aborted"},
                self._build_debug_context("scheduler", result),
            )
            commands = json.loads((self.log_dir / "scheduler" / "commands.json").read_text(encoding="utf-8"))
            self.assertTrue(any(("crontab -l" in entry["command"]) or ("launchctl list" in entry["command"]) or ("LaunchAgents" in entry["command"]) for entry in commands))
            server, base_url = self._start_server()
            try:
                html = urllib.request.urlopen(base_url + "/").read().decode("utf-8", errors="replace")
            finally:
                self._stop_server(server)
            route_files = sorted(str(path.relative_to(self.routes_dir)) for path in self.routes_dir.rglob("*") if path.is_file())
            self.assertTrue(
                "<table" in html or "scheduled" in html.lower(),
                f"Expected task list/table in root HTML.\nRoute files: {route_files}\nHTML (first 2000 chars):\n{html[:2000]}",
            )
            self.assertTrue(
                "form" in html.lower() or "schedule" in html.lower(),
                f"Expected scheduling form in root HTML.\nRoute files: {route_files}\nHTML (first 2000 chars):\n{html[:2000]}",
            )
            self.assertTrue(
                "confirm(" in html or "remove" in html.lower(),
                f"Expected destructive-action affordance in root HTML.\nRoute files: {route_files}\nHTML (first 2000 chars):\n{html[:2000]}",
            )

    def test_network_scanner_budget_is_sufficient(self):
            """Diagnose: does the network-scanner build exhaust its budget?"""
            budget = BudgetRef(remaining=int(os.environ.get("AGENT_LOOP_TEST_BUDGET", "25")))
            initial = budget.remaining
            result = self._run_build(
                (
                    "Create a web app that scans the local network on the server side, shows progress, "
                    "renders results in a table, and includes a topology graph."
                ),
                "network-scanner-diag",
            )
            result_type = result.get("type") if isinstance(result, dict) else "answer"
            log_path = self.log_dir / "network-scanner-diag"
            requests_path = log_path / "request.json"
            num_requests = len(json.loads(requests_path.read_text(encoding="utf-8"))) if requests_path.exists() else 0
            commands_path = log_path / "commands.json"
            num_commands = len(json.loads(commands_path.read_text(encoding="utf-8"))) if commands_path.exists() else 0

            self.assertNotEqual(
                "budget_exhausted", result_type,
                f"Budget exhausted after {num_requests} API calls and {num_commands} commands "
                f"(started with {initial}). Increase AGENT_LOOP_TEST_BUDGET or simplify the prompt.",
            )

    def test_scheduler_budget_is_sufficient(self):
            """Diagnose: does the scheduler build exhaust its budget before command review?"""
            budget = BudgetRef(remaining=int(os.environ.get("AGENT_LOOP_TEST_BUDGET", "25")))
            initial = budget.remaining
            result = self._run_build(
                (
                    "Create a web app frontend for cron and launchd task management on macOS, including "
                    "listing tasks, adding cron jobs, removing cron jobs, enabling and disabling launchd "
                    "agents, and confirming destructive actions."
                ),
                "scheduler-budget-diag",
            )
            result_type = result.get("type") if isinstance(result, dict) else "answer"
            log_path = self.log_dir / "scheduler-budget-diag"
            requests_path = log_path / "request.json"
            num_requests = len(json.loads(requests_path.read_text(encoding="utf-8"))) if requests_path.exists() else 0
            commands_path = log_path / "commands.json"
            num_commands = len(json.loads(commands_path.read_text(encoding="utf-8"))) if commands_path.exists() else 0

            self.assertNotEqual(
                "budget_exhausted", result_type,
                f"Budget exhausted after {num_requests} API calls and {num_commands} commands "
                f"(started with {initial}). Context:\n{self._build_debug_context('scheduler-budget-diag', result)}",
            )

    def test_qr_code_reader_html_contains_camera_elements(self):
            """Diagnose: what does the QR-reader index page actually contain?"""
            result = self._run_build(
                (
                    "Create a web app with a page that opens the device camera, detects QR codes in the "
                    "video feed, and displays the decoded contents. When a QR code is detected, present "
                    "actions that fit the content and always include Say it and Use as change prompt."
                ),
                "qr-diag",
            )
            result_type = result.get("type") if isinstance(result, dict) else "answer"
            if result_type in {"error", "budget_exhausted", "user_aborted"}:
                self.fail(f"Build did not complete: type={result_type}, result={result}")

            route_files = sorted(str(p.relative_to(self.routes_dir)) for p in self.routes_dir.rglob("*") if p.is_file())
            self.assertTrue(route_files, "No route files were created")

            server, base_url = self._start_server()
            try:
                html = urllib.request.urlopen(base_url + "/").read().decode("utf-8", errors="replace")
            finally:
                self._stop_server(server)

            snippet = html[:2000]
            has_video = "<video" in html
            has_getUserMedia = "getUserMedia" in html
            self.assertTrue(
                has_video or has_getUserMedia,
                f"Index HTML has neither <video nor getUserMedia.\n"
                f"Route files: {route_files}\n"
                f"HTML (first 2000 chars):\n{snippet}",
            )

    def test_qr_code_reader_root_is_not_directory_listing(self):
            """Diagnose: did the agent create only client-source files that fsrouter lists verbatim?"""
            result = self._run_build(
                (
                    "Create a web app with a page that opens the device camera, detects QR codes in the "
                    "video feed, and displays the decoded contents. When a QR code is detected, present "
                    "actions that fit the content and always include Say it and Use as change prompt."
                ),
                "qr-root-diag",
            )
            result_type = result.get("type") if isinstance(result, dict) else "answer"
            if result_type in {"error", "budget_exhausted", "user_aborted"}:
                self.fail(f"Build did not complete: {self._build_debug_context('qr-root-diag', result)}")

            route_files = sorted(str(path.relative_to(self.routes_dir)) for path in self.routes_dir.rglob("*") if path.is_file())
            server, base_url = self._start_server()
            try:
                html = urllib.request.urlopen(base_url + "/").read().decode("utf-8", errors="replace")
            finally:
                self._stop_server(server)

            self.assertNotIn(
                "<title>Index of /</title>",
                html,
                f"Root request returned a directory listing instead of an app page.\n"
                f"Route files: {route_files}\n"
                f"HTML (first 2000 chars):\n{html[:2000]}",
            )

    def test_scheduler_commands_include_system_probes(self):
            """Diagnose: what commands did the scheduler build actually run?"""
            result = self._run_build(
                (
                    "Create a web app frontend for cron and launchd task management on macOS, including "
                    "listing tasks, adding cron jobs, removing cron jobs, enabling and disabling launchd "
                    "agents, and confirming destructive actions."
                ),
                "scheduler-diag",
            )
            result_type = result.get("type") if isinstance(result, dict) else "answer"
            if result_type in {"error", "budget_exhausted", "user_aborted"}:
                self.fail(f"Build did not complete: type={result_type}, result={result}")

            commands_path = self.log_dir / "scheduler-diag" / "commands.json"
            self.assertTrue(commands_path.exists(), "commands.json was not written")
            commands = json.loads(commands_path.read_text(encoding="utf-8"))
            actual_cmds = [entry["command"] for entry in commands]

            expected_any = ["crontab -l", "launchctl list", "LaunchAgents"]
            found = [kw for kw in expected_any if any(kw in cmd for cmd in actual_cmds)]
            self.assertTrue(
                found,
                f"None of {expected_any} appear in the {len(actual_cmds)} commands that were run.\n"
                f"Actual commands:\n" + "\n".join(f"  - {c}" for c in actual_cmds),
            )

    def test_scheduler_root_is_not_directory_listing(self):
            """Diagnose: did the scheduler build produce files that fsrouter only lists?"""
            result = self._run_build(
                (
                    "Create a web app frontend for cron and launchd task management on macOS, including "
                    "listing tasks, adding cron jobs, removing cron jobs, enabling and disabling launchd "
                    "agents, and confirming destructive actions."
                ),
                "scheduler-root-diag",
            )
            result_type = result.get("type") if isinstance(result, dict) else "answer"
            if result_type in {"error", "budget_exhausted", "user_aborted"}:
                self.fail(f"Build did not complete: {self._build_debug_context('scheduler-root-diag', result)}")

            route_files = sorted(str(path.relative_to(self.routes_dir)) for path in self.routes_dir.rglob("*") if path.is_file())
            server, base_url = self._start_server()
            try:
                html = urllib.request.urlopen(base_url + "/").read().decode("utf-8", errors="replace")
            finally:
                self._stop_server(server)

            self.assertNotIn(
                "<title>Index of /</title>",
                html,
                f"Root request returned a directory listing instead of an app page.\n"
                f"Route files: {route_files}\n"
                f"HTML (first 2000 chars):\n{html[:2000]}",
            )


if __name__ == '__main__':
    unittest.main(verbosity=2)
