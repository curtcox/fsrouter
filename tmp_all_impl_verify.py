import os
import subprocess
import sys

REPO = "/Users/curt/me/fsrouter"
IMPLEMENTATIONS = ["go", "rust", "python", "ruby", "java", "deno", "groovy", "lua", "perl"]
TIMEOUT = 120

results = []

for impl in IMPLEMENTATIONS:
    env = dict(os.environ)
    if impl != "go":
        env["FSROUTER_IMPL"] = impl
    else:
        env.pop("FSROUTER_IMPL", None)

    print(f"=== {impl} ===")
    sys.stdout.flush()
    try:
        completed = subprocess.run(
            ["python3", "spec/test-suite/run.py"],
            cwd=REPO,
            env=env,
            capture_output=True,
            text=True,
            timeout=TIMEOUT,
        )
        stdout = completed.stdout
        stderr = completed.stderr
        sys.stdout.write(stdout)
        sys.stderr.write(stderr)
        results.append((impl, completed.returncode))
        if completed.returncode != 0:
            print(f"RESULT {impl} FAIL {completed.returncode}")
            break
        print(f"RESULT {impl} OK")
    except subprocess.TimeoutExpired as exc:
        sys.stdout.write(exc.stdout or "")
        sys.stderr.write(exc.stderr or "")
        print(f"RESULT {impl} TIMEOUT")
        results.append((impl, 124))
        break
    sys.stdout.flush()
    sys.stderr.flush()

print("=== SUMMARY ===")
for impl, code in results:
    print(f"{impl}: {code}")

if any(code != 0 for _, code in results) or len(results) != len(IMPLEMENTATIONS):
    sys.exit(1)
