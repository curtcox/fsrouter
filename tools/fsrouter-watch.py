#!/usr/bin/env python3
"""Restart an fsrouter command when the route tree changes.

This wrapper standardizes "hot reload" for development and lightweight local
use without requiring every implementation to embed a filesystem watcher.
"""

from __future__ import annotations

import argparse
import os
import signal
import stat
import subprocess
import sys
import time
from pathlib import Path


METHOD_NAMES = {"GET", "HEAD", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Restart an fsrouter server command when the route tree changes."
    )
    parser.add_argument(
        "--route-dir",
        default=os.environ.get("ROUTE_DIR", "./routes"),
        help="Route directory to monitor (default: ROUTE_DIR or ./routes).",
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=0.5,
        help="Polling interval in seconds (default: 0.5).",
    )
    parser.add_argument(
        "command",
        nargs=argparse.REMAINDER,
        help="Command to run, introduced by --.",
    )
    args = parser.parse_args()
    if args.command and args.command[0] == "--":
        args.command = args.command[1:]
    if not args.command:
        parser.error("missing command; use -- <command> [args...]")
    if args.interval <= 0:
        parser.error("--interval must be greater than 0")
    return args


def is_method_file(path: Path) -> bool:
    return path.name.upper() in METHOD_NAMES


def fingerprint_tree(route_dir: Path) -> tuple:
    if not route_dir.exists():
        return ("missing",)

    items: list[tuple] = []
    stack = [route_dir]
    seen_dirs: set[Path] = set()

    while stack:
        current = stack.pop()
        try:
            real_current = current.resolve()
        except OSError:
            real_current = current
        if real_current in seen_dirs:
            continue
        seen_dirs.add(real_current)

        try:
            rel_current = current.relative_to(route_dir).as_posix()
        except ValueError:
            rel_current = "."
        items.append(("dir", rel_current))

        try:
            entries = sorted(os.scandir(current), key=lambda entry: entry.name)
        except OSError as err:
            items.append(("dir-error", rel_current, type(err).__name__, str(err)))
            continue

        for entry in entries:
            entry_path = Path(entry.path)
            rel_path = entry_path.relative_to(route_dir).as_posix()

            try:
                is_dir = entry.is_dir(follow_symlinks=True)
            except OSError as err:
                items.append(("entry-error", rel_path, type(err).__name__, str(err)))
                continue

            if is_dir:
                stack.append(entry_path)
                continue

            if not is_method_file(entry_path):
                continue

            try:
                entry_stat = entry_path.stat()
                mode = stat.S_IMODE(entry_stat.st_mode)
            except OSError as err:
                items.append(("file-error", rel_path, type(err).__name__, str(err)))
                continue

            try:
                symlink_target = os.readlink(entry_path)
            except OSError:
                symlink_target = ""

            items.append(
                (
                    "method",
                    rel_path,
                    mode,
                    symlink_target,
                )
            )

    items.sort()
    return tuple(items)


def stop_process(process: subprocess.Popen[bytes], grace_seconds: float = 5.0) -> None:
    if process.poll() is not None:
        return

    process.terminate()
    deadline = time.monotonic() + grace_seconds
    while time.monotonic() < deadline:
        if process.poll() is not None:
            return
        time.sleep(0.05)

    if process.poll() is None:
        process.kill()
        process.wait()


def start_process(command: list[str]) -> subprocess.Popen[bytes]:
    sys.stderr.write(f"[fsrouter-watch] starting: {' '.join(command)}\n")
    sys.stderr.flush()
    return subprocess.Popen(command)


def main() -> int:
    args = parse_args()
    route_dir = Path(args.route_dir).resolve()
    command = args.command
    stopping = False

    def handle_signal(signum: int, _frame: object) -> None:
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    current_fingerprint = fingerprint_tree(route_dir)
    process = start_process(command)

    try:
        while not stopping:
            time.sleep(args.interval)

            next_fingerprint = fingerprint_tree(route_dir)
            if next_fingerprint != current_fingerprint:
                current_fingerprint = next_fingerprint
                sys.stderr.write(
                    f"[fsrouter-watch] change detected under {route_dir}; restarting\n"
                )
                sys.stderr.flush()
                stop_process(process)
                process = start_process(command)
                continue

            exit_code = process.poll()
            if exit_code is not None:
                return exit_code
    finally:
        stop_process(process)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
