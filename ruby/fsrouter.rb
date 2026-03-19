#!/usr/bin/env ruby
require 'json'
require 'find'
require 'open3'
require 'timeout'
require 'uri'
require 'webrick'

HTTP_METHODS = %w[GET HEAD POST PUT DELETE PATCH OPTIONS].freeze

class Node
  attr_accessor :literal, :param, :param_name, :handlers

  def initialize(param_name = '')
    @literal = {}
    @param = nil
    @param_name = param_name
    @handlers = {}
  end

  def match(segs)
    params = {}
    cur = self
    segs.each do |seg|
      if cur.literal.key?(seg)
        cur = cur.literal[seg]
      elsif cur.param
        params[cur.param.param_name] = seg
        cur = cur.param
      else
        return [nil, nil]
      end
    end
    [cur, params]
  end
end

def normalize_request_path(path)
  collapsed = path.gsub(%r{/+}, '/')
  collapsed = '/' if collapsed.empty?
  trimmed = collapsed == '/' ? collapsed : collapsed.sub(%r{/$}, '')
  segs = []
  trimmed.split('/').each do |segment|
    next if segment.empty?
    decoded = URI::DEFAULT_PARSER.unescape(segment)
    raise ArgumentError, 'invalid path' if decoded == '..'
    segs << decoded
  end
  segs
end

def is_executable(path)
  File.file?(path) && File.executable?(path)
end

def join_prefix(prefix, seg)
  prefix.empty? ? seg : "#{prefix}/#{seg}"
end

def build_tree(route_dir)
  abs_dir = File.realpath(route_dir)
  root = Node.new

  Find.find(abs_dir) do |path|
    next if File.directory?(path)

    method = File.basename(path).upcase
    next unless HTTP_METHODS.include?(method)

    rel_dir = File.dirname(path).delete_prefix(abs_dir)
    segs = rel_dir.split('/').reject(&:empty?)
    cur = root
    segs.each do |seg|
      if seg.start_with?(':')
        cur.param ||= Node.new(seg[1..-1])
        cur = cur.param
      else
        cur.literal[seg] ||= Node.new
        cur = cur.literal[seg]
      end
    end
    cur.handlers[method] = path
  end

  root
end

def collect_routes(node, prefix, items)
  route = prefix.empty? ? '/' : "/#{prefix}"
  node.handlers.each do |method, path|
    tag = begin
      is_executable(path) ? 'exec' : 'static'
    rescue StandardError
      'unknown'
    end
    items << [route, method, path, tag]
  end
  node.literal.keys.sort.each do |seg|
    collect_routes(node.literal[seg], join_prefix(prefix, seg), items)
  end
  collect_routes(node.param, join_prefix(prefix, ":#{node.param.param_name}"), items) if node.param
end

def print_routes(root, route_dir)
  $stderr.puts("routes from #{route_dir}:")
  items = []
  collect_routes(root, '', items)
  items.sort_by { |item| [item[0], item[1]] }.each do |route, method, path, tag|
    $stderr.puts(format('  %-7s %-45s → %s [%s]', method, route, path, tag))
  end
  $stderr.flush
end

def env_key(value)
  value.upcase.tr('-', '_')
end

def split_host_port(value)
  return ['', ''] if value.nil? || value.empty?

  if value.start_with?('[') && value.include?(']')
    close = value.index(']')
    host = value[1...close]
    rest = value[(close + 1)..]
    return [host, rest.start_with?(':') ? rest[1..-1] : '']
  end

  if value.count(':') == 1
    host, port = value.split(':', 2)
    return [host, port]
  end

  [value, '']
end

def build_env(req, server, params)
  env = ENV.to_h
  env['REQUEST_METHOD'] = req.request_method
  env['REQUEST_URI'] = req.request_uri.to_s
  env['REQUEST_PATH'] = req.path
  env['QUERY_STRING'] = req.query_string.to_s
  env['CONTENT_TYPE'] = req['content-type'].to_s
  env['CONTENT_LENGTH'] = req['content-length'].to_s
  env['REMOTE_ADDR'] = "#{req.peeraddr[3]}:#{req.peeraddr[1]}"

  server_name, server_port = split_host_port(req.host || server[:listen_addr])
  env['SERVER_NAME'] = server_name
  env['SERVER_PORT'] = server_port unless server_port.empty?

  params.each do |key, value|
    env["PARAM_#{env_key(key)}"] = value
  end

  seen_query = {}
  URI.decode_www_form(req.query_string.to_s).each do |key, value|
    next if seen_query[key]
    seen_query[key] = true
    env["QUERY_#{env_key(key)}"] = value
  end

  seen_headers = {}
  req.each do |key, value|
    next if seen_headers[key]
    seen_headers[key] = true
    env["HTTP_#{key.upcase.tr('-', '_')}"] = value.to_s
  end

  env
end

def looks_like_header(raw)
  raw.each_byte.with_index do |byte, idx|
    return true if byte == ':'.ord && idx.positive?
    return false if ["\n".ord, "\r".ord, ' '.ord, "\t".ord].include?(byte)
  end
  false
end

def parse_header_line(line)
  idx = line.index(':')
  return nil unless idx && idx.positive?

  key = line[0...idx]
  return nil if key.each_char.any? { |ch| ch.ord <= 32 || ch.ord == 127 }

  [key, line[(idx + 1)..].strip]
end

def parse_cgi_headers(raw, default_status)
  status = default_status
  content_type = 'application/json'
  headers = []
  body_index = nil
  lines = raw.split(/\n/, -1)
  offset = 0

  lines.each do |line|
    consumed = line.bytesize + 1
    line = line.delete_suffix("\r")
    if line.empty?
      body_index = offset + consumed
      break
    end

    parsed = parse_header_line(line.force_encoding('UTF-8'))
    return nil unless parsed

    key, value = parsed
    if key.downcase == 'status'
      first = value.split.first
      status = first.to_i if first && first.match?(/^\d+$/)
    elsif key.downcase == 'content-type'
      content_type = value
    else
      headers << [key, value]
    end
    offset += consumed
  end

  return nil unless body_index

  [status, content_type, headers, raw.byteslice(body_index..) || '']
end

def exit_to_status(code)
  return 200 if code == 0
  return 400 if code == 1

  500
end

def json_response(res, req, status, payload)
  body = JSON.generate(payload)
  res.status = status
  res['Content-Type'] = 'application/json'
  res['Content-Length'] = body.bytesize.to_s
  res.body = req.request_method == 'HEAD' ? '' : body
end

def serve_static(req, res, handler_path)
  data = File.binread(handler_path)
  res.status = 200
  res['Content-Type'] = WEBrick::HTTPUtils.mime_type(handler_path, WEBrick::HTTPUtils::DefaultMimeTypes)
  res['Content-Length'] = data.bytesize.to_s
  res.body = req.request_method == 'HEAD' ? '' : data
  200
rescue StandardError => e
  json_response(res, req, 500, { error: 'static_read_failed', message: e.message })
  500
end

def execute_handler(req, res, server, handler_path, params)
  env = build_env(req, server, params)
  body = req.body || ''
  stdout = ''
  stderr = ''
  status = nil
  wait_thr = nil
  pid = nil

  begin
    Open3.popen3(env, handler_path, chdir: File.dirname(handler_path)) do |stdin, out, err, thr|
      wait_thr = thr
      pid = thr.pid
      stdin.binmode
      out.binmode
      err.binmode
      stdin.write(body)
      stdin.close
      begin
        Timeout.timeout(server[:command_timeout]) do
          stdout_reader = Thread.new { out.read }
          stderr_reader = Thread.new { err.read }
          status = thr.value
          stdout = stdout_reader.value
          stderr = stderr_reader.value
        end
      rescue Timeout::Error
        begin
          Process.kill('TERM', pid)
        rescue StandardError
        end
        begin
          Timeout.timeout(1) { thr.value }
        rescue StandardError
          begin
            Process.kill('KILL', pid)
          rescue StandardError
          end
        end
        json_response(res, req, 504, { error: 'handler_timeout', timeout_seconds: server[:command_timeout] })
        return 504
      end
    end
  rescue Errno::ENOENT, Errno::EACCES => e
    json_response(res, req, 502, { error: 'exec_failed', message: e.message })
    return 502
  end

  unless stderr.empty?
    $stderr.puts("  [handler stderr] #{stderr.encode('UTF-8', invalid: :replace, undef: :replace).rstrip}")
    $stderr.flush
  end

  exit_code = status&.exitstatus || 0
  raw = stdout
  raw = stderr if raw.empty? && exit_code != 0 && !stderr.empty?
  write_cgi_response(req, res, raw, exit_code)
end

def write_cgi_response(req, res, raw, exit_code)
  status = exit_to_status(exit_code)
  content_type = 'application/json'
  headers = []
  body = raw

  if !raw.empty? && looks_like_header(raw)
    parsed = parse_cgi_headers(raw, status)
    if parsed
      status, content_type, headers, body = parsed
    end
  end

  res.status = status
  headers.each { |key, value| res[key] = value }
  res['Content-Type'] = content_type
  res['Content-Length'] = body.bytesize.to_s
  res.body = req.request_method == 'HEAD' ? '' : body
  status
end

def handle_handler(req, res, server, handler_path, params)
  return serve_static(req, res, handler_path) unless is_executable(handler_path)

  execute_handler(req, res, server, handler_path, params)
rescue StandardError => e
  json_response(res, req, 500, { error: 'handler_stat_failed', message: e.message })
  500
end

def parse_listen_addr(addr)
  return ['0.0.0.0', addr[1..].to_i] if addr.start_with?(':')
  if addr.start_with?('[') && addr.include?(']')
    host, port = split_host_port(addr)
    return [host, port.to_i]
  end
  if addr.count(':') == 1
    host, port = addr.split(':', 2)
    return [host, port.to_i]
  end
  [addr, 8080]
end

def env_or(key, fallback)
  value = ENV[key]
  value.nil? || value.empty? ? fallback : value
end

def process_request(req, res, root, server_config)
  start = Process.clock_gettime(Process::CLOCK_MONOTONIC)
  status = 500

  begin
    segs = normalize_request_path(req.path)
    node, params = root.match(segs)
    if node.nil? || node.handlers.empty?
      json_response(res, req, 404, { error: 'not_found', path: req.path })
      status = 404
    else
      handler_path = node.handlers[req.request_method]
      handler_path ||= node.handlers['GET'] if req.request_method == 'HEAD'
      if handler_path.nil?
        allowed = node.handlers.keys.sort
        json_response(res, req, 405, { error: 'method_not_allowed', allow: allowed })
        res['Allow'] = allowed.join(', ')
        status = 405
      else
        status = handle_handler(req, res, server_config, handler_path, params || {})
      end
    end
  rescue ArgumentError
    json_response(res, req, 400, { error: 'invalid_path', path: req.path })
    status = 400
  end

  elapsed = Process.clock_gettime(Process::CLOCK_MONOTONIC) - start
  $stderr.puts(format('%s %s → %d (%.6fs)', req.request_method, req.path, status, elapsed))
  $stderr.flush
end

class FsrouterServlet < WEBrick::HTTPServlet::AbstractServlet
  def initialize(server, root, server_config)
    super(server)
    @root = root
    @server_config = server_config
  end

  def do_GET(req, res)
    process_request(req, res)
  end

  def do_HEAD(req, res)
    process_request(req, res)
  end

  def do_POST(req, res)
    process_request(req, res)
  end

  def do_PUT(req, res)
    process_request(req, res)
  end

  def do_DELETE(req, res)
    process_request(req, res)
  end

  def do_PATCH(req, res)
    process_request(req, res)
  end

  def do_OPTIONS(req, res)
    process_request(req, res)
  end

  private

  def process_request(req, res)
    Object.new.send(:process_request, req, res, @root, @server_config)
  end
end

route_dir = env_or('ROUTE_DIR', './routes')
listen_addr = env_or('LISTEN_ADDR', ':8080')
timeout_seconds = Integer(env_or('COMMAND_TIMEOUT', '30'), exception: false) || 30
timeout_seconds = 30 if timeout_seconds <= 0

begin
  root = build_tree(route_dir)
rescue StandardError => e
  $stderr.puts("failed to scan #{route_dir}: #{e.message}")
  $stderr.flush
  exit 1
end

print_routes(root, route_dir)
host, port = parse_listen_addr(listen_addr)
server_config = { root: root, command_timeout: timeout_seconds, listen_addr: listen_addr }

server = WEBrick::HTTPServer.new(
  BindAddress: host,
  Port: port,
  AccessLog: [],
  Logger: WEBrick::Log.new($stderr, WEBrick::Log::WARN)
)

trap('INT') { server.shutdown }
trap('TERM') { server.shutdown }

server.mount('/', FsrouterServlet, root, server_config)

$stderr.puts("listening on #{listen_addr} (timeout #{timeout_seconds}s)")
$stderr.flush
server.start
