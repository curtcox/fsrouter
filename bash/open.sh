#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
FSROUTER_SCRIPT="$SCRIPT_DIR/fsrouter.sh"
STATE_DIR="${HOME}/Library/Caches/fsrouter-open"
WORK_DIR="$STATE_DIR/work"
CLONE_DIR="$WORK_DIR/clones"
EXTRACT_DIR="$WORK_DIR/extracts"

usage() {
  printf 'usage: %s <file-or-url>\n' "$(basename "$0")" >&2
  exit 1
}

require_macos() {
  if [[ "$(uname -s)" != 'Darwin' ]]; then
    printf 'this helper only supports macOS\n' >&2
    exit 1
  fi
}

ensure_dirs() {
  mkdir -p "$STATE_DIR" "$WORK_DIR" "$CLONE_DIR" "$EXTRACT_DIR"
}

is_git_url() {
  local value="$1"
  [[ "$value" =~ ^git@ ]] || [[ "$value" =~ ^ssh:// ]] || [[ "$value" =~ ^git:// ]] || [[ "$value" =~ ^https?://.+\.git/?$ ]]
}

is_archive_path() {
  local value
  value=$(printf '%s' "$1" | tr '[:upper:]' '[:lower:]')
  [[ "$value" == *.zip || "$value" == *.tar || "$value" == *.tar.gz || "$value" == *.tgz || "$value" == *.tar.bz2 || "$value" == *.tbz2 || "$value" == *.tar.xz || "$value" == *.txz || "$value" == *.gz || "$value" == *.bz2 || "$value" == *.xz ]]
}

abs_path() {
  python3 -c 'import os,sys; print(os.path.abspath(sys.argv[1]))' "$1"
}

safe_name() {
  python3 -c 'import hashlib,sys; value=sys.argv[1].encode(); print(hashlib.sha256(value).hexdigest()[:16])' "$1"
}

pick_free_port() {
  python3 -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1]); s.close()'
}

wait_for_server() {
  local port="$1"
  python3 - "$port" <<'PY'
import socket
import sys
import time
port = int(sys.argv[1])
deadline = time.time() + 10
while time.time() < deadline:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.2):
            raise SystemExit(0)
    except OSError:
        time.sleep(0.05)
raise SystemExit(1)
PY
}

open_when_server_ready() {
  local port="$1"
  local url="$2"
  if wait_for_server "$port"; then
    open "$url"
  fi
}

normalize_extracted_root() {
  python3 - "$1" <<'PY'
import os
import sys
root = os.path.abspath(sys.argv[1])
entries = [name for name in os.listdir(root) if name not in {'.DS_Store'}]
if len(entries) == 1:
    child = os.path.join(root, entries[0])
    if os.path.isdir(child):
        print(child)
        raise SystemExit(0)
print(root)
PY
}

extract_archive() {
  local source="$1"
  local hash target root
  hash=$(safe_name "$source")
  target="$EXTRACT_DIR/$hash"
  rm -rf "$target"
  mkdir -p "$target"
  case "$(printf '%s' "$source" | tr '[:upper:]' '[:lower:]')" in
    *.zip)
      unzip -qq "$source" -d "$target"
      ;;
    *.tar|*.tar.gz|*.tgz|*.tar.bz2|*.tbz2|*.tar.xz|*.txz)
      tar -xf "$source" -C "$target"
      ;;
    *.gz)
      gunzip -c "$source" > "$target/$(basename "${source%.gz}")"
      ;;
    *.bz2)
      bunzip2 -c "$source" > "$target/$(basename "${source%.bz2}")"
      ;;
    *.xz)
      xz -dc "$source" > "$target/$(basename "${source%.xz}")"
      ;;
    *)
      printf 'unsupported archive type: %s\n' "$source" >&2
      exit 1
      ;;
  esac
  root=$(normalize_extracted_root "$target")
  printf '%s\n' "$root"
}

clone_or_update_repo() {
  local url="$1"
  local hash repo_dir current_branch upstream
  hash=$(safe_name "$url")
  repo_dir="$CLONE_DIR/$hash"
  if [[ ! -d "$repo_dir/.git" ]]; then
    git clone "$url" "$repo_dir" >&2
  else
    git -C "$repo_dir" fetch --all --prune >&2
    current_branch=$(git -C "$repo_dir" rev-parse --abbrev-ref HEAD 2>/dev/null || true)
    if [[ -n "$current_branch" && "$current_branch" != 'HEAD' ]]; then
      upstream=$(git -C "$repo_dir" rev-parse --abbrev-ref --symbolic-full-name '@{u}' 2>/dev/null || true)
      if [[ -n "$upstream" ]]; then
        git -C "$repo_dir" pull --ff-only >&2 || true
      fi
    fi
  fi
  printf '%s\n' "$repo_dir"
}

resolve_route_root() {
  local input="$1"
  if is_git_url "$input"; then
    clone_or_update_repo "$input"
    return 0
  fi

  local path
  path=$(abs_path "$input")
  if [[ -d "$path" ]]; then
    printf '%s\n' "$path"
    return 0
  fi
  if [[ -f "$path" ]]; then
    if is_archive_path "$path"; then
      extract_archive "$path"
    else
      dirname "$path"
    fi
    return 0
  fi

  printf 'input does not exist or is unsupported: %s\n' "$input" >&2
  exit 1
}

start_server() {
  local route_root="$1"
  local port url
  port=$(pick_free_port)
  url="http://127.0.0.1:${port}/"
  printf 'route root: %s\n' "$route_root"
  printf 'server url: %s\n' "$url"
  printf 'press Ctrl-C to stop the server\n'
  open_when_server_ready "$port" "$url" &
  exec env ROUTE_DIR="$route_root" LISTEN_ADDR="127.0.0.1:${port}" COMMAND_TIMEOUT="${COMMAND_TIMEOUT:-30}" bash "$FSROUTER_SCRIPT"
}

main() {
  require_macos
  ensure_dirs
  [[ $# -eq 1 ]] || usage
  [[ -f "$FSROUTER_SCRIPT" ]] || { printf 'missing server script: %s\n' "$FSROUTER_SCRIPT" >&2; exit 1; }
  local route_root
  route_root=$(resolve_route_root "$1")
  start_server "$route_root"
}

main "$@"
