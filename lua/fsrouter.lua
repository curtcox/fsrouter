local socket = require("socket")
local system = require("system")

local HTTP_METHODS = {
  GET = true,
  HEAD = true,
  POST = true,
  PUT = true,
  DELETE = true,
  PATCH = true,
  OPTIONS = true,
}

local MIME_TYPES = {
  [".json"] = "application/json",
  [".txt"] = "text/plain",
  [".html"] = "text/html",
  [".js"] = "application/javascript",
  [".css"] = "text/css",
  [".xml"] = "application/xml",
  [".png"] = "image/png",
  [".jpg"] = "image/jpeg",
  [".jpeg"] = "image/jpeg",
  [".gif"] = "image/gif",
  [".svg"] = "image/svg+xml",
}

local path_sep = package.config:sub(1, 1)

local function json_escape(value)
  return value:gsub("\\", "\\\\"):gsub('"', '\\"'):gsub("\b", "\\b"):gsub("\f", "\\f"):gsub("\n", "\\n"):gsub("\r", "\\r"):gsub("\t", "\\t")
end

local function json_encode(value)
  local t = type(value)
  if t == "nil" then
    return "null"
  end
  if t == "boolean" then
    return value and "true" or "false"
  end
  if t == "number" then
    return tostring(value)
  end
  if t == "string" then
    return '"' .. json_escape(value) .. '"'
  end
  if t == "table" then
    local is_array = true
    local max_index = 0
    for key, _ in pairs(value) do
      if type(key) ~= "number" or key < 1 or key % 1 ~= 0 then
        is_array = false
        break
      end
      if key > max_index then
        max_index = key
      end
    end
    local parts = {}
    if is_array then
      for i = 1, max_index do
        parts[#parts + 1] = json_encode(value[i])
      end
      return "[" .. table.concat(parts, ",") .. "]"
    end
    for key, item in pairs(value) do
      parts[#parts + 1] = json_encode(tostring(key)) .. ":" .. json_encode(item)
    end
    table.sort(parts)
    return "{" .. table.concat(parts, ",") .. "}"
  end
  error("unsupported json type: " .. t)
end

local function env_or(key, fallback)
  local value = os.getenv(key)
  if value == nil or value == "" then
    return fallback
  end
  return value
end

local function parse_timeout(value)
  local parsed = tonumber(value)
  if not parsed or parsed <= 0 then
    return 30
  end
  return math.floor(parsed)
end

local function trim_trailing_slash(path)
  if path == "/" then
    return "/"
  end
  return (path:gsub("/+$", ""))
end

local function percent_decode(segment)
  return (segment:gsub("%%(%x%x)", function(hex)
    return string.char(tonumber(hex, 16))
  end))
end

local function normalize_request_path(path)
  local collapsed = path:gsub("/+", "/")
  if collapsed == "" then
    collapsed = "/"
  end
  local trimmed = trim_trailing_slash(collapsed)
  local segs = {}
  for segment in trimmed:gmatch("[^/]+") do
    local decoded = percent_decode(segment)
    if decoded == ".." then
      error("invalid_path")
    end
    segs[#segs + 1] = decoded
  end
  return segs
end

local function path_join(...)
  local parts = { ... }
  return table.concat(parts, path_sep)
end

local function dirname(path)
  local normalized = path:gsub(path_sep .. "+$", "")
  local match = normalized:match("^(.*)" .. path_sep .. "[^" .. path_sep .. "]+$")
  if not match or match == "" then
    return "."
  end
  return match
end

local function basename(path)
  return path:match("[^" .. path_sep .. "]+$") or path
end

local function split_ext(path)
  local ext = path:match("(%.[^%." .. path_sep .. "]+)$")
  return ext
end

local function is_executable(path)
  local process = io.popen("test -x " .. string.format("%q", path) .. " && printf 1 || printf 0", "r")
  if not process then
    return false
  end
  local out = process:read("*a") or ""
  process:close()
  return out:match("1") ~= nil
end

local function list_tree(root)
  local cmd = "find " .. string.format("%q", root) .. " -type f -print"
  local process = assert(io.popen(cmd, "r"))
  local items = {}
  for line in process:lines() do
    items[#items + 1] = line
  end
  local ok = process:close()
  if ok == nil then
    error("failed to scan route dir")
  end
  table.sort(items)
  return items
end

local function new_node(param_name)
  return {
    literal = {},
    literal_order = {},
    param = nil,
    param_name = param_name or "",
    handlers = {},
  }
end

local function build_tree(route_dir)
  local root = new_node()
  local files = list_tree(route_dir)
  for _, file_path in ipairs(files) do
    local method = string.upper(basename(file_path))
    if HTTP_METHODS[method] then
      local rel_dir = dirname(file_path):sub(#route_dir + 1)
      rel_dir = rel_dir:gsub("^" .. path_sep .. "+", "")
      local cur = root
      if rel_dir ~= "" and rel_dir ~= "." then
        for seg in rel_dir:gmatch("[^" .. path_sep .. "]+") do
          if seg:sub(1, 1) == ":" then
            if not cur.param then
              cur.param = new_node(seg:sub(2))
            end
            cur = cur.param
          else
            if not cur.literal[seg] then
              cur.literal[seg] = new_node()
              cur.literal_order[#cur.literal_order + 1] = seg
              table.sort(cur.literal_order)
            end
            cur = cur.literal[seg]
          end
        end
      end
      cur.handlers[method] = file_path
    end
  end
  return root
end

local function match_node(root, segs)
  local params = {}
  local cur = root
  for _, seg in ipairs(segs) do
    if cur.literal[seg] then
      cur = cur.literal[seg]
    elseif cur.param then
      params[cur.param.param_name] = seg
      cur = cur.param
    else
      return nil, nil
    end
  end
  return cur, params
end

local function join_prefix(prefix, seg)
  if prefix == "" then
    return seg
  end
  return prefix .. "/" .. seg
end

local function collect_routes(node, prefix, items)
  local route = prefix == "" and "/" or "/" .. prefix
  for method, file_path in pairs(node.handlers) do
    local tag = is_executable(file_path) and "exec" or "static"
    items[#items + 1] = { route = route, method = method, path = file_path, tag = tag }
  end
  table.sort(node.literal_order)
  for _, seg in ipairs(node.literal_order) do
    collect_routes(node.literal[seg], join_prefix(prefix, seg), items)
  end
  if node.param then
    collect_routes(node.param, join_prefix(prefix, ":" .. node.param.param_name), items)
  end
end

local function print_routes(root, route_dir)
  io.stderr:write("routes from " .. route_dir .. ":\n")
  local items = {}
  collect_routes(root, "", items)
  table.sort(items, function(a, b)
    if a.route == b.route then
      return a.method < b.method
    end
    return a.route < b.route
  end)
  for _, item in ipairs(items) do
    io.stderr:write(string.format("  %-7s %-45s → %s [%s]\n", item.method, item.route, item.path, item.tag))
  end
  io.stderr:flush()
end

local function split_host_port(value)
  if not value or value == "" then
    return "", ""
  end
  if value:sub(1, 1) == "[" then
    local host, port = value:match("^%[([^%]]+)%]%:(.+)$")
    if host then
      return host, port
    end
    local bare = value:match("^%[([^%]]+)%]$")
    return bare or "", ""
  end
  local a, b = value:match("^(.-):([^:]+)$")
  if a and b and not a:find(":", 1, true) then
    return a, b
  end
  return value, ""
end

local function env_key(value)
  return string.upper((value:gsub("-", "_")))
end

local function parse_query(raw_query)
  local out = {}
  if not raw_query or raw_query == "" then
    return out
  end
  for pair in raw_query:gmatch("[^&]+") do
    local key, value = pair:match("^([^=]*)=(.*)$")
    if key == nil then
      key = pair
      value = ""
    end
    key = percent_decode(key:gsub("+", " "))
    value = percent_decode(value:gsub("+", " "))
    out[#out + 1] = { key = key, value = value }
  end
  return out
end

local function build_env(request, params, listen_addr)
  local env = {}
  for key, value in pairs(request.server_env or {}) do
    env[key] = value
  end
  env.REQUEST_METHOD = request.method
  env.REQUEST_URI = request.target
  env.REQUEST_PATH = request.path
  env.QUERY_STRING = request.query or ""
  env.CONTENT_TYPE = request.headers["content-type"] or ""
  env.CONTENT_LENGTH = request.headers["content-length"] or ""
  env.REMOTE_ADDR = request.remote_addr or ""

  local server_name, server_port = split_host_port(request.headers.host or listen_addr)
  env.SERVER_NAME = server_name
  if server_port ~= "" then
    env.SERVER_PORT = server_port
  end

  for key, value in pairs(params) do
    env["PARAM_" .. env_key(key)] = value
  end

  local seen_query = {}
  for _, item in ipairs(parse_query(request.query)) do
    if not seen_query[item.key] then
      seen_query[item.key] = true
      env["QUERY_" .. env_key(item.key)] = item.value
    end
  end

  local seen_headers = {}
  for key, value in pairs(request.headers) do
    local lower = string.lower(key)
    if not seen_headers[lower] then
      seen_headers[lower] = true
      env["HTTP_" .. string.upper((key:gsub("-", "_")))] = value
    end
  end

  return env
end

local function exit_to_status(code)
  if code == 0 then
    return 200
  end
  if code == 1 then
    return 400
  end
  return 500
end

local function looks_like_header(raw)
  for i = 1, #raw do
    local byte = raw:byte(i)
    if byte == string.byte(":") and i > 1 then
      return true
    end
    if byte == string.byte("\n") or byte == string.byte("\r") or byte == string.byte(" ") or byte == string.byte("\t") then
      return false
    end
  end
  return false
end

local function parse_header_line(line)
  local key, value = line:match("^([^:]+):%s*(.*)$")
  if not key or key == "" then
    return nil
  end
  for i = 1, #key do
    local b = key:byte(i)
    if b <= 32 or b == 127 then
      return nil
    end
  end
  return key, value
end

local function parse_cgi_headers(raw, default_status)
  local status = default_status
  local content_type = "application/json"
  local headers = {}
  local pos = 1
  local saw_blank = false

  while pos <= #raw do
    local newline = raw:find("\n", pos, true)
    local line
    local next_pos
    if newline then
      line = raw:sub(pos, newline - 1)
      next_pos = newline + 1
    else
      line = raw:sub(pos)
      next_pos = #raw + 1
    end
    if line:sub(-1) == "\r" then
      line = line:sub(1, -2)
    end
    if line == "" then
      saw_blank = true
      pos = next_pos
      break
    end
    local key, value = parse_header_line(line)
    if not key then
      return nil
    end
    if string.lower(key) == "status" then
      local first = value:match("^(%d+)")
      if first then
        status = tonumber(first) or status
      end
    elseif string.lower(key) == "content-type" then
      content_type = value
    else
      headers[#headers + 1] = { key = key, value = value }
    end
    pos = next_pos
  end

  if not saw_blank then
    return nil
  end

  return {
    status = status,
    content_type = content_type,
    headers = headers,
    body = raw:sub(pos),
  }
end

local function content_type_for(path)
  local ext = split_ext(path)
  return MIME_TYPES[ext] or "application/octet-stream"
end

local function read_file(path)
  local file = assert(io.open(path, "rb"))
  local data = file:read("*a") or ""
  file:close()
  return data
end

local function write_file(path, data)
  local file = assert(io.open(path, "wb"))
  file:write(data)
  file:close()
end

local function shell_quote(value)
  return string.format("%q", value)
end

local function run_handler(handler_path, request_body, env, timeout_seconds)
  local stdin_path = os.tmpname()
  local stdout_path = os.tmpname()
  local stderr_path = os.tmpname()
  write_file(stdin_path, request_body)

  local env_parts = {}
  for key, value in pairs(env) do
    env_parts[#env_parts + 1] = key .. "=" .. shell_quote(value)
  end
  table.sort(env_parts)

  local cwd = dirname(handler_path)
  local command = table.concat({
    "cd ", shell_quote(cwd),
    " && env -i ", table.concat(env_parts, " "),
    " perl -e ", shell_quote("alarm shift; exec @ARGV"),
    " ", shell_quote(tostring(timeout_seconds)),
    " ", shell_quote(handler_path),
    " <", shell_quote(stdin_path),
    " >", shell_quote(stdout_path),
    " 2>", shell_quote(stderr_path),
  })

  local ok, reason, code = os.execute(command)
  local stdout = ""
  local stderr = ""
  pcall(function() stdout = read_file(stdout_path) end)
  pcall(function() stderr = read_file(stderr_path) end)
  os.remove(stdin_path)
  os.remove(stdout_path)
  os.remove(stderr_path)

  local exit_code = 0
  if type(code) == "number" then
    exit_code = code
  elseif ok == true then
    exit_code = 0
  else
    exit_code = 1
  end

  local timed_out = exit_code == 142 or exit_code == 124
  return {
    ok = ok,
    reason = reason,
    exit_code = exit_code,
    stdout = stdout,
    stderr = stderr,
    timed_out = timed_out,
  }
end

local function status_reason(status)
  local map = {
    [200] = "OK",
    [400] = "Bad Request",
    [404] = "Not Found",
    [405] = "Method Not Allowed",
    [500] = "Internal Server Error",
    [502] = "Bad Gateway",
    [504] = "Gateway Timeout",
  }
  return map[status] or "OK"
end

local function send_response(client, method, status, headers, body)
  headers["Content-Length"] = tostring(#body)
  headers["Connection"] = "close"
  client:send(string.format("HTTP/1.1 %d %s\r\n", status, status_reason(status)))
  for key, value in pairs(headers) do
    client:send(key .. ": " .. value .. "\r\n")
  end
  client:send("\r\n")
  if method ~= "HEAD" then
    client:send(body)
  end
end

local function write_json(client, method, status, payload, extra_headers)
  local body = json_encode(payload)
  local headers = extra_headers or {}
  headers["Content-Type"] = "application/json"
  send_response(client, method, status, headers, body)
  return status
end

local function write_cgi_response(client, method, raw, exit_code)
  local status = exit_to_status(exit_code)
  local content_type = "application/json"
  local extra_headers = {}
  local body = raw
  if #raw > 0 and looks_like_header(raw) then
    local parsed = parse_cgi_headers(raw, status)
    if parsed then
      status = parsed.status
      content_type = parsed.content_type
      for _, item in ipairs(parsed.headers) do
        extra_headers[item.key] = item.value
      end
      body = parsed.body
    end
  end
  extra_headers["Content-Type"] = content_type
  send_response(client, method, status, extra_headers, body)
  return status
end

local function serve_static(client, method, handler_path)
  local ok, data = pcall(read_file, handler_path)
  if not ok then
    return write_json(client, method, 500, { error = "static_read_failed", message = tostring(data) })
  end
  send_response(client, method, 200, { ["Content-Type"] = content_type_for(handler_path) }, data)
  return 200
end

local function is_dir(path)
  local process = io.popen("test -d " .. string.format("%q", path) .. " && printf 1 || printf 0", "r")
  if not process then return false end
  local out = process:read("*a") or ""
  process:close()
  return out:match("1") ~= nil
end

local function is_file(path)
  local f = io.open(path, "r")
  if f then f:close() return true end
  return false
end

local function list_dir_entries(path)
  local process = io.popen("ls -1 " .. string.format("%q", path) .. " 2>/dev/null", "r")
  if not process then return {} end
  local entries = {}
  for line in process:lines() do
    entries[#entries + 1] = line
  end
  process:close()
  table.sort(entries)
  return entries
end

local function serve_dir_listing(client, method, dir_path, request_path)
  local entries = list_dir_entries(dir_path)
  local title = "Index of " .. (request_path or "/")
  local parts = { "<!DOCTYPE html><html><head><title>" .. title .. "</title></head><body><h1>" .. title .. "</h1><ul>" }
  if request_path and request_path ~= "/" then
    parts[#parts + 1] = '<li><a href="../">../</a></li>'
  end
  for _, name in ipairs(entries) do
    local full = dir_path .. "/" .. name
    if is_dir(full) then
      parts[#parts + 1] = '<li><a href="' .. name .. '/">' .. name .. '/</a></li>'
    else
      parts[#parts + 1] = '<li><a href="' .. name .. '">' .. name .. '</a></li>'
    end
  end
  parts[#parts + 1] = "</ul></body></html>"
  local body = table.concat(parts, "\n")
  send_response(client, method, 200, { ["Content-Type"] = "text/html; charset=utf-8" }, body)
  return 200
end

local handle_handler

local function find_directory_index(dir_path)
  for _, name in ipairs({ "index.html", "index.htm" }) do
    local candidate = dir_path .. "/" .. name
    if is_file(candidate) then
      return "static", candidate
    end
  end

  local entries = list_dir_entries(dir_path)
  for _, name in ipairs(entries) do
    local candidate = dir_path .. "/" .. name
    if name:match("^index%.") and is_file(candidate) and is_executable(candidate) then
      return "exec", candidate
    end
  end
  return nil, nil
end

local function serve_filesystem_fallback(client, request, segs, route_dir, timeout_seconds, listen_addr)
  local parts = { route_dir }
  for _, seg in ipairs(segs) do
    parts[#parts + 1] = seg
  end
  local fallback = table.concat(parts, "/")
  if is_dir(fallback) then
    local kind, candidate = find_directory_index(fallback)
    if kind == "static" then
      return serve_static(client, request.method, candidate)
    end
    if kind == "exec" then
      return handle_handler(client, request, candidate, {}, timeout_seconds, listen_addr)
    end
    return serve_dir_listing(client, request.method, fallback, request.path)
  end
  if is_file(fallback) then
    return serve_static(client, request.method, fallback)
  end
  return write_json(client, request.method, 404, { error = "not_found", path = request.path })
end

local function parse_request(client)
  local request_line, err = client:receive("*l")
  if not request_line then
    return nil, err
  end
  local method, target, version = request_line:match("^(%S+)%s+(%S+)%s+(HTTP/%d+%.%d+)$")
  if not method then
    return nil, "bad_request"
  end
  local headers = {}
  while true do
    local line, line_err = client:receive("*l")
    if not line then
      return nil, line_err
    end
    if line == "" then
      break
    end
    local key, value = line:match("^([^:]+):%s*(.*)$")
    if key then
      headers[string.lower(key)] = value
      headers[key] = value
    end
  end
  local path, query = target:match("^([^?]*)%??(.*)$")
  local length = tonumber(headers["content-length"] or "0") or 0
  local body = ""
  if length > 0 then
    body = client:receive(length) or ""
  end
  return {
    method = string.upper(method),
    target = target,
    version = version,
    headers = headers,
    path = path,
    query = query,
    body = body,
  }
end

local function log_result(method, path, status, start_time)
  local elapsed = socket.gettime() - start_time
  io.stderr:write(string.format("%s %s → %d (%.6fs)\n", method, path, status, elapsed))
  io.stderr:flush()
end

handle_handler = function(client, request, handler_path, params, timeout_seconds, listen_addr)
  if not is_executable(handler_path) then
    return serve_static(client, request.method, handler_path)
  end
  local env = build_env(request, params, listen_addr)
  local result = run_handler(handler_path, request.body, env, timeout_seconds)
  if result.timed_out then
    return write_json(client, request.method, 504, { error = "handler_timeout", timeout_seconds = timeout_seconds })
  end
  if not result.ok and result.exit_code == 0 then
    return write_json(client, request.method, 502, { error = "exec_failed", message = tostring(result.reason) })
  end
  if result.stderr ~= "" then
    io.stderr:write("  [handler stderr] " .. result.stderr:gsub("[\r\n]+$", "") .. "\n")
    io.stderr:flush()
  end
  local raw = result.stdout
  if raw == "" and result.exit_code ~= 0 and result.stderr ~= "" then
    raw = result.stderr
  end
  return write_cgi_response(client, request.method, raw, result.exit_code)
end

local function parse_listen_addr(addr)
  if addr:sub(1, 1) == ":" then
    return "*", tonumber(addr:sub(2)) or 8080
  end
  local host, port = split_host_port(addr)
  if port ~= "" then
    return host, tonumber(port) or 8080
  end
  return addr, 8080
end

local function snapshot_env()
  return system.getenvs()
end

local function realpath(path)
  local process = io.popen("realpath " .. string.format("%q", path) .. " 2>/dev/null", "r")
  if not process then return path end
  local result = process:read("*l") or path
  process:close()
  return result ~= "" and result or path
end

local function main()
  local route_dir = env_or("ROUTE_DIR", "./routes")
  local listen_addr = env_or("LISTEN_ADDR", ":8080")
  local timeout_seconds = parse_timeout(env_or("COMMAND_TIMEOUT", "30"))
  local ok, root = pcall(build_tree, route_dir)
  if not ok then
    io.stderr:write("failed to scan " .. route_dir .. ": " .. tostring(root) .. "\n")
    io.stderr:flush()
    return 1
  end
  local abs_route_dir = realpath(route_dir)

  print_routes(root, route_dir)
  local host, port = parse_listen_addr(listen_addr)
  local server = assert(socket.bind(host, port))
  server:settimeout(0.2)
  io.stderr:write(string.format("listening on %s (timeout %ds)\n", listen_addr, timeout_seconds))
  io.stderr:flush()

  local server_env = snapshot_env()

  while true do
    local client = server:accept()
    if client then
      client:settimeout(5)
      local start_time = socket.gettime()
      local request, err = parse_request(client)
      if request then
        request.remote_addr = tostring((select(1, client:getpeername()))) .. ":" .. tostring((select(2, client:getpeername())))
        request.server_env = server_env
        local status
        local ok_handle, handle_err = pcall(function()
          local segs = normalize_request_path(request.path)
          local node, params = match_node(root, segs)
          if not node or next(node.handlers) == nil then
            status = serve_filesystem_fallback(client, request, segs, abs_route_dir, timeout_seconds, listen_addr)
            return
          end
          local handler_path = node.handlers[request.method]
          if not handler_path and request.method == "HEAD" then
            handler_path = node.handlers.GET
          end
          if not handler_path then
            local allow = {}
            for method, _ in pairs(node.handlers) do
              allow[#allow + 1] = method
            end
            table.sort(allow)
            status = write_json(client, request.method, 405, { error = "method_not_allowed", allow = allow }, { ["Allow"] = table.concat(allow, ", ") })
            return
          end
          status = handle_handler(client, request, handler_path, params or {}, timeout_seconds, listen_addr)
        end)
        if not ok_handle then
          if tostring(handle_err):find("invalid_path", 1, true) then
            status = write_json(client, request.method, 400, { error = "invalid_path", path = request.path })
          else
            status = write_json(client, request.method, 500, { error = "server_error", message = tostring(handle_err) })
          end
        end
        log_result(request.method, request.path, status, start_time)
      elseif err ~= "timeout" then
        write_json(client, "GET", 400, { error = "bad_request" })
      end
      client:close()
    end
  end
end

os.exit(main())
