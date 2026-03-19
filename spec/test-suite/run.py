from pathlib import Path
import sys
import unittest


def main() -> int:
    tests_dir = Path(__file__).resolve().parent / "tests"
    suite = unittest.defaultTestLoader.discover(start_dir=str(tests_dir), pattern="test_*.py")
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
