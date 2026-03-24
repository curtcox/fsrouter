#!/usr/bin/env python3
from pathlib import Path
import sys

APP_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP_ROOT / "lib"))

from ai_change_app import worker_main


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("usage: process_change.py <change-id>")
    raise SystemExit(worker_main(sys.argv[1]))
