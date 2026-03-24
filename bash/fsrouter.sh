#!/usr/bin/env bash
set -u

HTTP_METHODS="GET HEAD POST PUT DELETE PATCH OPTIONS"
ROUTE_TABLE=""
ROUTE_DIR_ABS=""
NC_PID=""

contains_method() {
  local needle="$1"
  local method
  for method in $HTTP_METHODS; do
    if [[ "$method" == "$needle" ]]; then
      return 0
    fi
  done
  return 1
}

env_or() {
  local key="$1"
  local fallback="$2"
  local value="${!key-}"
  if [[ -n "$value" ]]; then
    printf '%s' "$value"
  else
    printf '%s' "$fallback"
  fi
}

parse_timeout() {
  local value="$1"
  if [[ "$value" =~ ^[0-9]+$ ]] && (( value > 0 )); then
    printf '%s' "$value"
  else
    printf '30'
  fi
}

json_escape() {
  local value="$1"
  value=${value//\\/\\\\}
  value=${value//"/\\"}
  value=${value//$'\n'/\\n}
  value=${value//$'\r'/\\r}
  value=${value//$'\t'/\\t}
  printf '%s' "$value"
}

json_string() {
  printf '"%s"' "$(json_escape "$1")"
}

url_decode() {
  local value="${1//+/ }"
  printf '%b' "${value//%/\\x}"
}

normalize_request_path() {
  local path="$1"
  local collapsed
  collapsed=$(printf '%s' "$path" | sed -E 's:/+:/:g')
  if [[ -z "$collapsed" ]]; then
    collapsed='/'
  fi
  if [[ "$collapsed" != "/" ]]; then
    collapsed="${collapsed%/}"
  fi
  local IFS='/'
  read -r -a raw_parts <<< "$collapsed"
  local part
  local out=()
  for part in "${raw_parts[@]}"; do
    [[ -z "$part" ]] && continue
    part=$(url_decode "$part")
    if [[ "$part" == ".." ]]; then
      return 1
    fi
    out+=("$part")
  done
  printf '%s\n' "${out[@]}"
}

join_by() {
  local sep="$1"
  shift
  local first=1
  local item
  for item in "$@"; do
    if (( first )); then
      printf '%s' "$item"
      first=0
    else
      printf '%s%s' "$sep" "$item"
    fi
  done
}

build_route_table() {
  local route_dir="$1"
  ROUTE_DIR_ABS=$(python3 -c 'import os,sys; print(os.path.abspath(sys.argv[1]))' "$route_dir") || return 1
  [[ -d "$ROUTE_DIR_ABS" ]] || return 1
  ROUTE_TABLE=$(mktemp)
  while IFS= read -r path; do
    local filename method rel dir route kind
    filename=$(basename "$path")
    method=$(printf '%s' "$filename" | tr '[:lower:]' '[:upper:]')
    contains_method "$method" || continue
    dir=$(dirname "$path")
    rel=$(python3 -c 'import os,sys; print(os.path.relpath(sys.argv[1], sys.argv[2]))' "$dir" "$ROUTE_DIR_ABS") || continue
    if [[ "$rel" == "." ]]; then
      route='/'
    else
      route="/$rel"
    fi
    if [[ -x "$path" ]]; then
      kind='exec'
    else
      kind='static'
    fi
    printf '%s\t%s\t%s\t%s\n' "$route" "$method" "$path" "$kind" >> "$ROUTE_TABLE"
  done < <(find "$ROUTE_DIR_ABS" -type f | sort)
}

print_routes() {
  local route_dir="$1"
  printf 'routes from %s:\n' "$route_dir" >&2
  while IFS=$'\t' read -r route method path kind; do
    printf '  %-7s %-45s → %s [%s]\n' "$method" "$route" "$path" "$kind" >&2
  done < "$ROUTE_TABLE"
}

split_host_port() {
  local value="$1"
  if [[ -z "$value" ]]; then
    printf '\t\n'
  elif [[ "$value" =~ ^\[([^]]+)\]:(.+)$ ]]; then
    printf '%s\t%s\n' "${BASH_REMATCH[1]}" "${BASH_REMATCH[2]}"
  elif [[ "$value" =~ ^([^:]+):(.*)$ ]]; then
    printf '%s\t%s\n' "${BASH_REMATCH[1]}" "${BASH_REMATCH[2]}"
  else
    printf '%s\t\n' "$value"
  fi
}

env_key() {
  local value="$1"
  value=${value//-/_}
  printf '%s' "$(printf '%s' "$value" | tr '[:lower:]' '[:upper:]')"
}

status_reason() {
  case "$1" in
    200) printf 'OK' ;;
    400) printf 'Bad Request' ;;
    404) printf 'Not Found' ;;
    405) printf 'Method Not Allowed' ;;
    500) printf 'Internal Server Error' ;;
    502) printf 'Bad Gateway' ;;
    504) printf 'Gateway Timeout' ;;
    *) printf 'OK' ;;
  esac
}

exit_to_status() {
  case "$1" in
    0) printf '200' ;;
    1) printf '400' ;;
    *) printf '500' ;;
  esac
}

content_type_for() {
  local path="$1"
  case "$path" in
    *.json) printf 'application/json' ;;
    *.txt) printf 'text/plain' ;;
    *.html) printf 'text/html' ;;
    *.js) printf 'application/javascript' ;;
    *.css) printf 'text/css' ;;
    *.xml) printf 'application/xml' ;;
    *.png) printf 'image/png' ;;
    *.jpg|*.jpeg) printf 'image/jpeg' ;;
    *.gif) printf 'image/gif' ;;
    *.svg) printf 'image/svg+xml' ;;
    *) printf 'application/octet-stream' ;;
  esac
}

log_result() {
  local method="$1"
  local path="$2"
  local status="$3"
  local started="$4"
  python3 -c 'import sys,time; start=float(sys.argv[4]); sys.stderr.write(f"{sys.argv[1]} {sys.argv[2]} → {sys.argv[3]} ({time.time()-start:.6f}s)\\n")' "$method" "$path" "$status" "$started" >&2
}

write_response_file() {
  local file="$1"
  local method="$2"
  local status="$3"
  local content_type="$4"
  local body_file="$5"
  local extra_headers_file="$6"
  local body_len
  body_len=$(wc -c < "$body_file" | tr -d ' ')
  {
    printf 'HTTP/1.1 %s %s\r\n' "$status" "$(status_reason "$status")"
    printf 'Content-Length: %s\r\n' "$body_len"
    printf 'Connection: close\r\n'
    if [[ -n "$content_type" ]]; then
      printf 'Content-Type: %s\r\n' "$content_type"
    fi
    if [[ -f "$extra_headers_file" ]]; then
      while IFS= read -r line; do
        [[ -n "$line" ]] && printf '%s\r\n' "$line"
      done < "$extra_headers_file"
    fi
    printf '\r\n'
    if [[ "$method" != 'HEAD' ]]; then
      cat "$body_file"
    fi
  } > "$file"
}

write_json_response_file() {
  local file="$1"
  local method="$2"
  local status="$3"
  local json="$4"
  local body_file extra_headers
  body_file=$(mktemp)
  extra_headers=$(mktemp)
  printf '%s' "$json" > "$body_file"
  : > "$extra_headers"
  write_response_file "$file" "$method" "$status" 'application/json' "$body_file" "$extra_headers"
  rm -f "$body_file" "$extra_headers"
}

match_route() {
  local path="$1"
  MATCH_FOUND=0
  MATCH_ROUTE=''
  MATCH_ALLOW=''
  MATCH_HANDLER=''
  MATCH_KIND=''
  MATCH_PARAMS_FILE=$(mktemp)
  local normalized
  normalized=$(normalize_request_path "$path") || return 2
  local req_parts=()
  if [[ -n "$normalized" ]]; then
    while IFS= read -r line; do
      [[ -n "$line" ]] && req_parts+=("$line")
    done <<< "$normalized"
  fi
  local req_count=${#req_parts[@]}
  local best_score=-1
  local line route method handler kind
  while IFS=$'\t' read -r route method handler kind; do
    local route_trimmed="${route#/}"
    local route_parts=()
    if [[ "$route" != '/' ]]; then
      local seg
      IFS='/' read -r -a route_parts <<< "$route_trimmed"
      local filtered=()
      for seg in "${route_parts[@]}"; do
        [[ -n "$seg" ]] && filtered+=("$seg")
      done
      route_parts=("${filtered[@]}")
    fi
    (( ${#route_parts[@]} == req_count )) || continue
    local i literal_score=0 ok=1
    local tmp_params
    tmp_params=$(mktemp)
    for ((i=0; i<req_count; i++)); do
      local rseg="${route_parts[$i]}"
      local qseg="${req_parts[$i]}"
      if [[ "$rseg" == :* ]]; then
        printf '%s\t%s\n' "${rseg#:}" "$qseg" >> "$tmp_params"
      elif [[ "$rseg" == "$qseg" ]]; then
        literal_score=$((literal_score + 1))
      else
        ok=0
        break
      fi
    done
    if (( ok )) && (( literal_score > best_score )); then
      best_score=$literal_score
      MATCH_FOUND=1
      MATCH_ROUTE="$route"
      cp "$tmp_params" "$MATCH_PARAMS_FILE"
    fi
    rm -f "$tmp_params"
  done < "$ROUTE_TABLE"
  if (( MATCH_FOUND == 0 )); then
    return 1
  fi
  local methods=()
  while IFS=$'\t' read -r route method handler kind; do
    if [[ "$route" == "$MATCH_ROUTE" ]]; then
      methods+=("$method")
    fi
  done < "$ROUTE_TABLE"
  MATCH_ALLOW=$(join_by ', ' "${methods[@]}")
  return 0
}

find_handler_for_method() {
  local route="$1"
  local method="$2"
  local line r m handler kind
  while IFS=$'\t' read -r r m handler kind; do
    if [[ "$r" == "$route" && "$m" == "$method" ]]; then
      MATCH_HANDLER="$handler"
      MATCH_KIND="$kind"
      return 0
    fi
  done < "$ROUTE_TABLE"
  return 1
}

build_env_script() {
  local file="$1"
  local request_method="$2"
  local request_target="$3"
  local request_path="$4"
  local query_string="$5"
  local content_type="$6"
  local content_length="$7"
  local remote_addr="$8"
  local host_header="$9"
  local listen_addr="${10}"
  {
    printf 'export REQUEST_METHOD=%q\n' "$request_method"
    printf 'export REQUEST_URI=%q\n' "$request_target"
    printf 'export REQUEST_PATH=%q\n' "$request_path"
    printf 'export QUERY_STRING=%q\n' "$query_string"
    printf 'export CONTENT_TYPE=%q\n' "$content_type"
    printf 'export CONTENT_LENGTH=%q\n' "$content_length"
    printf 'export REMOTE_ADDR=%q\n' "$remote_addr"
    local host_port_parts server_name server_port
    host_port_parts=$(split_host_port "${host_header:-$listen_addr}")
    server_name=${host_port_parts%%$'\t'*}
    server_port=${host_port_parts#*$'\t'}
    printf 'export SERVER_NAME=%q\n' "$server_name"
    if [[ -n "$server_port" ]]; then
      printf 'export SERVER_PORT=%q\n' "$server_port"
    fi
    while IFS=$'\t' read -r key value; do
      [[ -z "$key" ]] && continue
      printf 'export PARAM_%s=%q\n' "$(env_key "$key")" "$value"
    done < "$MATCH_PARAMS_FILE"
    local pair seen_queries
    seen_queries=$(mktemp)
    if [[ -n "$query_string" ]]; then
      IFS='&' read -r -a pairs <<< "$query_string"
      for pair in "${pairs[@]}"; do
        local k v k_decoded v_decoded envname
        k=${pair%%=*}
        if [[ "$pair" == *=* ]]; then
          v=${pair#*=}
        else
          v=''
        fi
        k_decoded=$(url_decode "$k")
        v_decoded=$(url_decode "$v")
        envname="QUERY_$(env_key "$k_decoded")"
        if ! grep -qxF "$envname" "$seen_queries" 2>/dev/null; then
          printf '%s\n' "$envname" >> "$seen_queries"
          printf 'export %s=%q\n' "$envname" "$v_decoded"
        fi
      done
    fi
    rm -f "$seen_queries"
    local seen_headers
    seen_headers=$(mktemp)
    while IFS=$'\t' read -r key value; do
      [[ -z "$key" ]] && continue
      local envname="HTTP_$(env_key "$key")"
      if ! grep -qxF "$envname" "$seen_headers" 2>/dev/null; then
        printf '%s\n' "$envname" >> "$seen_headers"
        printf 'export %s=%q\n' "$envname" "$value"
      fi
    done < "$REQUEST_HEADERS_FILE"
    rm -f "$seen_headers"
  } > "$file"
}

run_handler() {
  local handler_path="$1"
  local request_body_file="$2"
  local request_method="$3"
  local request_target="$4"
  local request_path="$5"
  local query_string="$6"
  local content_type="$7"
  local content_length="$8"
  local remote_addr="$9"
  local host_header="${10}"
  local listen_addr="${11}"
  local timeout_seconds="${12}"
  local body_out stderr_out env_script rc cwd
  body_out=$(mktemp)
  stderr_out=$(mktemp)
  env_script=$(mktemp)
  build_env_script "$env_script" "$request_method" "$request_target" "$request_path" "$query_string" "$content_type" "$content_length" "$remote_addr" "$host_header" "$listen_addr"
  cwd=$(dirname "$handler_path")
  (
    source "$env_script"
    cd "$cwd" || exit 127
    perl -e 'alarm shift; exec @ARGV' "$timeout_seconds" "$handler_path" < "$request_body_file" > "$body_out" 2> "$stderr_out"
  )
  rc=$?
  HANDLER_EXIT_CODE=$rc
  HANDLER_STDOUT_FILE="$body_out"
  HANDLER_STDERR_FILE="$stderr_out"
  rm -f "$env_script"
}

parse_cgi_response() {
  local raw_file="$1"
  CGI_STATUS="$2"
  CGI_CONTENT_TYPE='application/json'
  CGI_BODY_FILE=$(mktemp)
  CGI_HEADERS_FILE=$(mktemp)
  python3 -c 'import sys
raw_path, body_path, headers_path, default_status = sys.argv[1:5]
raw = open(raw_path, "rb").read()
status = int(default_status)
content_type = "application/json"
headers = []
body = raw
pos = 0
saw_blank = False
parsed = False
if raw:
    while pos < len(raw):
        newline = raw.find(b"\n", pos)
        if newline == -1:
            line = raw[pos:]
            next_pos = len(raw)
        else:
            line = raw[pos:newline]
            next_pos = newline + 1
        if line.endswith(b"\r"):
            line = line[:-1]
        if line == b"":
            saw_blank = True
            pos = next_pos
            parsed = True
            break
        try:
            text = line.decode("utf-8")
        except UnicodeDecodeError:
            parsed = False
            break
        if ":" not in text:
            parsed = False
            break
        key, value = text.split(":", 1)
        if not key or any(ord(ch) <= 32 or ord(ch) == 127 for ch in key):
            parsed = False
            break
        value = value.strip()
        low = key.lower()
        if low == "status":
            first = value.split()
            if first:
                try:
                    status = int(first[0])
                except ValueError:
                    pass
        elif low == "content-type":
            content_type = value
        else:
            headers.append((key, value))
        pos = next_pos
    if parsed and saw_blank:
        body = raw[pos:]
with open(body_path, "wb") as fh:
    fh.write(body)
with open(headers_path, "w", encoding="utf-8") as fh:
    for key, value in headers:
        fh.write(f"{key}: {value}\n")
print(status)
print(content_type)
print(1 if parsed and saw_blank else 0)' "$raw_file" "$CGI_BODY_FILE" "$CGI_HEADERS_FILE" "$CGI_STATUS" > "$CGI_BODY_FILE.meta"
  CGI_STATUS=$(sed -n '1p' "$CGI_BODY_FILE.meta")
  CGI_CONTENT_TYPE=$(sed -n '2p' "$CGI_BODY_FILE.meta")
  rm -f "$CGI_BODY_FILE.meta"
}

serve_static() {
  local response_file="$1"
  local method="$2"
  local handler_path="$3"
  local extra_headers
  extra_headers=$(mktemp)
  : > "$extra_headers"
  write_response_file "$response_file" "$method" 200 "$(content_type_for "$handler_path")" "$handler_path" "$extra_headers"
  rm -f "$extra_headers"
  printf '200'
}

find_directory_index() {
  local dir_path="$1"
  DIRECTORY_INDEX_KIND=''
  DIRECTORY_INDEX_PATH=''
  local candidate
  for candidate in "$dir_path/index.html" "$dir_path/index.htm"; do
    if [[ -f "$candidate" ]]; then
      DIRECTORY_INDEX_KIND='static'
      DIRECTORY_INDEX_PATH="$candidate"
      return 0
    fi
  done
  local name
  while IFS= read -r name; do
    [[ "$name" == index.* ]] || continue
    candidate="$dir_path/$name"
    if [[ -f "$candidate" && -x "$candidate" ]]; then
      DIRECTORY_INDEX_KIND='exec'
      DIRECTORY_INDEX_PATH="$candidate"
      return 0
    fi
  done < <(ls -1 "$dir_path" 2>/dev/null | sort)
  return 1
}

serve_executable_fallback() {
  local response_file="$1"
  local handler_path="$2"
  local request_body_file="$3"
  local request_method="$4"
  local request_target="$5"
  local request_path="$6"
  local query_string="$7"
  local content_type="$8"
  local content_length="$9"
  local peer="${10}"
  local host_header="${11}"
  local listen_addr="${12}"
  local timeout_seconds="${13}"
  run_handler "$handler_path" "$request_body_file" "$request_method" "$request_target" "$request_path" "$query_string" "$content_type" "$content_length" "$peer" "$host_header" "$listen_addr" "$timeout_seconds"
  if (( HANDLER_EXIT_CODE == 142 || HANDLER_EXIT_CODE == 124 )); then
    write_json_response_file "$response_file" "$request_method" 504 "{\"error\":\"handler_timeout\",\"timeout_seconds\":$timeout_seconds}"
    rm -f "$HANDLER_STDOUT_FILE" "$HANDLER_STDERR_FILE"
    printf '504'
    return
  fi
  if (( HANDLER_EXIT_CODE == 127 )); then
    write_json_response_file "$response_file" "$request_method" 502 '{"error":"exec_failed","message":"exec_failed"}'
    rm -f "$HANDLER_STDOUT_FILE" "$HANDLER_STDERR_FILE"
    printf '502'
    return
  fi
  local raw_for_parse
  raw_for_parse="$HANDLER_STDOUT_FILE"
  if [[ ! -s "$raw_for_parse" && "$HANDLER_EXIT_CODE" != '0' && -s "$HANDLER_STDERR_FILE" ]]; then
    raw_for_parse="$HANDLER_STDERR_FILE"
  fi
  parse_cgi_response "$raw_for_parse" "$(exit_to_status "$HANDLER_EXIT_CODE")"
  write_response_file "$response_file" "$request_method" "$CGI_STATUS" "$CGI_CONTENT_TYPE" "$CGI_BODY_FILE" "$CGI_HEADERS_FILE"
  if [[ -s "$HANDLER_STDERR_FILE" ]]; then
    local stderr_text
    stderr_text=$(tr -d '\r' < "$HANDLER_STDERR_FILE" | tr '\n' ' ')
    printf '  [handler stderr] %s\n' "$stderr_text" >&2
  fi
  rm -f "$CGI_BODY_FILE" "$CGI_HEADERS_FILE" "$HANDLER_STDOUT_FILE" "$HANDLER_STDERR_FILE"
  printf '%s' "$CGI_STATUS"
}

serve_executable_plain_file() {
  local response_file="$1"
  local handler_path="$2"
  local request_body_file="$3"
  local request_method="$4"
  local request_target="$5"
  local request_path="$6"
  local query_string="$7"
  local content_type="$8"
  local content_length="$9"
  local peer="${10}"
  local host_header="${11}"
  local listen_addr="${12}"
  local timeout_seconds="${13}"
  run_handler "$handler_path" "$request_body_file" "$request_method" "$request_target" "$request_path" "$query_string" "$content_type" "$content_length" "$peer" "$host_header" "$listen_addr" "$timeout_seconds"
  if (( HANDLER_EXIT_CODE == 142 || HANDLER_EXIT_CODE == 124 )); then
    write_json_response_file "$response_file" "$request_method" 504 "{\"error\":\"handler_timeout\",\"timeout_seconds\":$timeout_seconds}"
    rm -f "$HANDLER_STDOUT_FILE" "$HANDLER_STDERR_FILE"
    printf '504'
    return
  fi
  if (( HANDLER_EXIT_CODE == 127 )); then
    write_json_response_file "$response_file" "$request_method" 502 '{"error":"exec_failed","message":"exec_failed"}'
    rm -f "$HANDLER_STDOUT_FILE" "$HANDLER_STDERR_FILE"
    printf '502'
    return
  fi
  local extra_headers
  extra_headers=$(mktemp)
  : > "$extra_headers"
  write_response_file "$response_file" "$request_method" "$(exit_to_status "$HANDLER_EXIT_CODE")" 'text/plain' "$HANDLER_STDOUT_FILE" "$extra_headers"
  if [[ -s "$HANDLER_STDERR_FILE" ]]; then
    local stderr_text
    stderr_text=$(tr -d '\r' < "$HANDLER_STDERR_FILE" | tr '\n' ' ')
    printf '  [handler stderr] %s\n' "$stderr_text" >&2
  fi
  rm -f "$extra_headers" "$HANDLER_STDOUT_FILE" "$HANDLER_STDERR_FILE"
  printf '%s' "$(exit_to_status "$HANDLER_EXIT_CODE")"
}

serve_dir_listing() {
  local response_file="$1"
  local method="$2"
  local dir_path="$3"
  local request_path="$4"
  local title="Index of $request_path"
  local body_file
  body_file=$(mktemp)
  printf '<!DOCTYPE html><html><head><title>%s</title></head><body><h1>%s</h1><ul>' "$title" "$title" > "$body_file"
  if [[ "$request_path" != '/' ]]; then
    printf '<li><a href="../">../</a></li>' >> "$body_file"
  fi
  local name
  while IFS= read -r name; do
    [[ -z "$name" ]] && continue
    if [[ -d "$dir_path/$name" ]]; then
      printf '<li><a href="%s/">%s/</a></li>' "$name" "$name" >> "$body_file"
    else
      printf '<li><a href="%s">%s</a></li>' "$name" "$name" >> "$body_file"
    fi
  done < <(ls -1 "$dir_path" 2>/dev/null | sort)
  printf '</ul></body></html>' >> "$body_file"
  local extra_headers
  extra_headers=$(mktemp)
  : > "$extra_headers"
  write_response_file "$response_file" "$method" 200 'text/html; charset=utf-8' "$body_file" "$extra_headers"
  rm -f "$body_file" "$extra_headers"
  printf '200'
}

serve_filesystem_fallback() {
  local response_file="$1"
  local method="$2"
  local request_path="$3"
  local normalized="$4"
  local request_body_file="$5"
  local request_target="$6"
  local query_string="$7"
  local content_type="$8"
  local content_length="$9"
  local peer="${10}"
  local host_header="${11}"
  local listen_addr="${12}"
  local timeout_seconds="${13}"
  local route_dir_abs="$ROUTE_DIR_ABS"
  local fallback="$route_dir_abs"
  local seg
  if [[ -n "$normalized" ]]; then
    while IFS= read -r seg; do
      [[ -n "$seg" ]] && fallback="$fallback/$seg"
    done <<< "$normalized"
  fi
  if [[ -d "$fallback" ]]; then
    if find_directory_index "$fallback"; then
      if [[ -x "$DIRECTORY_INDEX_PATH" ]]; then
        serve_executable_plain_file "$response_file" "$DIRECTORY_INDEX_PATH" "$request_body_file" "$method" "$request_target" "$request_path" "$query_string" "$content_type" "$content_length" "$peer" "$host_header" "$listen_addr" "$timeout_seconds"
        return
      fi
      if [[ "$DIRECTORY_INDEX_KIND" == 'static' ]]; then
        serve_static "$response_file" "$method" "$DIRECTORY_INDEX_PATH"
        return
      fi
      serve_executable_plain_file "$response_file" "$DIRECTORY_INDEX_PATH" "$request_body_file" "$method" "$request_target" "$request_path" "$query_string" "$content_type" "$content_length" "$peer" "$host_header" "$listen_addr" "$timeout_seconds"
      return
    fi
    serve_dir_listing "$response_file" "$method" "$fallback" "$request_path"
    return
  fi
  if [[ -f "$fallback" ]]; then
    if [[ -x "$fallback" ]]; then
      serve_executable_plain_file "$response_file" "$fallback" "$request_body_file" "$method" "$request_target" "$request_path" "$query_string" "$content_type" "$content_length" "$peer" "$host_header" "$listen_addr" "$timeout_seconds"
      return
    fi
    serve_static "$response_file" "$method" "$fallback"
    return
  fi
  local escaped_path
  escaped_path=$(json_escape "$request_path")
  write_json_response_file "$response_file" "$method" 404 "{\"error\":\"not_found\",\"path\":\"$escaped_path\"}"
  printf '404'
}

handle_request() {
  local req_fifo="$1"
  local resp_fifo="$2"
  local peer="$3"
  local listen_addr="$4"
  local timeout_seconds="$5"
  local started
  started=$(python3 -c 'import time; print(time.time())')
  REQUEST_HEADERS_FILE=$(mktemp)
  local request_line
  if ! IFS= read -r -u 3 request_line; then
    rm -f "$REQUEST_HEADERS_FILE"
    return 0
  fi
  request_line=${request_line%$'\r'}
  local request_method request_target request_version
  if [[ ! "$request_line" =~ ^([^[:space:]]+)[[:space:]]+([^[:space:]]+)[[:space:]]+(HTTP/[0-9.]+)$ ]]; then
    local bad_request_file
    bad_request_file=$(mktemp)
    write_json_response_file "$bad_request_file" 'GET' 400 '{"error":"bad_request"}'
    cat "$bad_request_file" >&4
    rm -f "$bad_request_file"
    rm -f "$REQUEST_HEADERS_FILE"
    return 0
  fi
  request_method="${BASH_REMATCH[1]}"
  request_target="${BASH_REMATCH[2]}"
  request_version="${BASH_REMATCH[3]}"
  request_method=$(printf '%s' "$request_method" | tr '[:lower:]' '[:upper:]')
  local line
  while IFS= read -r -u 3 line; do
    line=${line%$'\r'}
    [[ -z "$line" ]] && break
    if [[ "$line" == *:* ]]; then
      local key="${line%%:*}"
      local value="${line#*:}"
      value="${value# }"
      printf '%s\t%s\n' "$(printf '%s' "$key" | tr '[:upper:]' '[:lower:]')" "$value" >> "$REQUEST_HEADERS_FILE"
    fi
  done
  local request_path="$request_target"
  local query_string=''
  if [[ "$request_target" == *\?* ]]; then
    request_path="${request_target%%\?*}"
    query_string="${request_target#*\?}"
  fi
  [[ -n "$request_path" ]] || request_path='/'
  local content_length='0'
  local content_type=''
  local host_header=''
  while IFS=$'\t' read -r key value; do
    [[ "$key" == 'content-length' ]] && content_length="$value"
    [[ "$key" == 'content-type' ]] && content_type="$value"
    [[ "$key" == 'host' ]] && host_header="$value"
  done < "$REQUEST_HEADERS_FILE"
  [[ "$content_length" =~ ^[0-9]+$ ]] || content_length='0'
  local request_body_file
  request_body_file=$(mktemp)
  if (( content_length > 0 )); then
    dd bs=1 count="$content_length" <&3 > "$request_body_file" 2>/dev/null
  else
    : > "$request_body_file"
  fi
  local response_tmp
  response_tmp=$(mktemp)
  local status='500'
  local normalized_segs
  normalized_segs=$(normalize_request_path "$request_path") || true
  if ! match_route "$request_path"; then
    status=$(serve_filesystem_fallback "$response_tmp" "$request_method" "$request_path" "$normalized_segs" "$request_body_file" "$request_target" "$query_string" "$content_type" "$content_length" "$peer" "$host_header" "$listen_addr" "$timeout_seconds")
  else
    local chosen_method="$request_method"
    if ! find_handler_for_method "$MATCH_ROUTE" "$chosen_method"; then
      if [[ "$request_method" == 'HEAD' ]] && find_handler_for_method "$MATCH_ROUTE" 'GET'; then
        chosen_method='GET'
      else
        local allow_json='['
        local method first=1
        IFS=', ' read -r -a allow_methods <<< "$MATCH_ALLOW"
        for method in "${allow_methods[@]}"; do
          [[ -z "$method" ]] && continue
          if (( first )); then
            allow_json+="\"$method\""
            first=0
          else
            allow_json+=",\"$method\""
          fi
        done
        allow_json+=']'
        local body_file extra_headers
        body_file=$(mktemp)
        extra_headers=$(mktemp)
        printf '{"error":"method_not_allowed","allow":%s}' "$allow_json" > "$body_file"
        printf 'Allow: %s\n' "$MATCH_ALLOW" > "$extra_headers"
        write_response_file "$response_tmp" "$request_method" 405 'application/json' "$body_file" "$extra_headers"
        rm -f "$body_file" "$extra_headers" "$request_body_file" "$REQUEST_HEADERS_FILE" "$MATCH_PARAMS_FILE"
        log_result "$request_method" "$request_path" '405' "$started"
        cat "$response_tmp" >&4
        rm -f "$response_tmp"
        return 0
      fi
    fi
    if [[ "$MATCH_KIND" == 'static' ]]; then
      status=$(serve_static "$response_tmp" "$request_method" "$MATCH_HANDLER")
    else
      status=$(serve_executable_fallback "$response_tmp" "$MATCH_HANDLER" "$request_body_file" "$request_method" "$request_target" "$request_path" "$query_string" "$content_type" "$content_length" "$peer" "$host_header" "$listen_addr" "$timeout_seconds")
    fi
  fi
  log_result "$request_method" "$request_path" "$status" "$started"
  cat "$response_tmp" >&4
  rm -f "$response_tmp" "$request_body_file" "$REQUEST_HEADERS_FILE" "$MATCH_PARAMS_FILE"
}

parse_listen_addr() {
  local addr="$1"
  if [[ "$addr" =~ ^:([0-9]+)$ ]]; then
    printf '0.0.0.0\t%s\n' "${BASH_REMATCH[1]}"
  elif [[ "$addr" =~ ^\[([^]]+)\]:([0-9]+)$ ]]; then
    printf '%s\t%s\n' "${BASH_REMATCH[1]}" "${BASH_REMATCH[2]}"
  elif [[ "$addr" =~ ^([^:]+):([0-9]+)$ ]]; then
    printf '%s\t%s\n' "${BASH_REMATCH[1]}" "${BASH_REMATCH[2]}"
  else
    printf '%s\t8080\n' "$addr"
  fi
}

cleanup() {
  if [[ -n "${NC_PID:-}" ]]; then
    kill "$NC_PID" 2>/dev/null
    wait "$NC_PID" 2>/dev/null
    NC_PID=""
  fi
  exec 3>&- 2>/dev/null
  exec 4>&- 2>/dev/null
  [[ -n "${ROUTE_TABLE:-}" && -f "$ROUTE_TABLE" ]] && rm -f "$ROUTE_TABLE"
  [[ -n "${SERVER_TMPDIR:-}" && -d "$SERVER_TMPDIR" ]] && rm -rf "$SERVER_TMPDIR"
}

main() {
  local route_dir listen_addr timeout_seconds
  route_dir=$(env_or 'ROUTE_DIR' './routes')
  listen_addr=$(env_or 'LISTEN_ADDR' ':8080')
  timeout_seconds=$(parse_timeout "$(env_or 'COMMAND_TIMEOUT' '30')")
  if ! build_route_table "$route_dir"; then
    printf 'failed to scan %s\n' "$route_dir" >&2
    return 1
  fi
  print_routes "$route_dir"
  local host_port host port
  host_port=$(parse_listen_addr "$listen_addr")
  host=${host_port%%$'\t'*}
  port=${host_port#*$'\t'}
  SERVER_TMPDIR=$(mktemp -d)
  trap 'cleanup; exit 0' INT TERM
  trap 'cleanup' EXIT
  printf 'listening on %s (timeout %ss)\n' "$listen_addr" "$timeout_seconds" >&2
  local req_fifo="$SERVER_TMPDIR/request.fifo"
  local resp_fifo="$SERVER_TMPDIR/response.fifo"
  rm -f "$req_fifo" "$resp_fifo"
  mkfifo "$req_fifo" "$resp_fifo"
  exec 3<> "$req_fifo"
  exec 4<> "$resp_fifo"
  nc -lk "$host" "$port" <&4 >&3 &
  NC_PID=$!
  while true; do
    handle_request "$req_fifo" "$resp_fifo" "" "$listen_addr" "$timeout_seconds"
  done
}

main "$@"
