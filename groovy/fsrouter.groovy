import com.sun.net.httpserver.Headers
import com.sun.net.httpserver.HttpExchange
import com.sun.net.httpserver.HttpHandler
import com.sun.net.httpserver.HttpServer
import groovy.json.JsonOutput
import groovy.transform.Field

import java.net.InetSocketAddress
import java.net.URI
import java.net.URLDecoder
import java.nio.charset.StandardCharsets
import java.nio.file.FileVisitResult
import java.nio.file.FileVisitor
import java.nio.file.FileVisitOption
import java.nio.file.Files
import java.nio.file.Path
import java.nio.file.Paths
import java.nio.file.attribute.BasicFileAttributes
import java.nio.file.attribute.PosixFilePermission
import java.time.Duration
import java.util.concurrent.Callable
import java.util.concurrent.ExecutionException
import java.util.concurrent.ExecutorService
import java.util.concurrent.Executors
import java.util.concurrent.Future
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit

@Field final Set<String> HTTP_METHODS = ["GET", "HEAD", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"] as Set<String>

class Node {
    final Map<String, Node> literal = new TreeMap<>()
    Node param
    String paramName = ""
    final Map<String, Path> handlers = new TreeMap<>()

    Map match(List<String> segs) {
        Map<String, String> params = new LinkedHashMap<>()
        Node cur = this
        for (String seg : segs) {
            Node next = cur.literal.get(seg)
            if (next != null) {
                cur = next
                continue
            }
            if (cur.param != null) {
                params.put(cur.param.paramName, seg)
                cur = cur.param
                continue
            }
            return [node: null, params: null]
        }
        [node: cur, params: params]
    }
}

class RouteItem {
    String route
    String method
    Path path
    String tag
}

class HostPort {
    String host
    String port
}

class ListenAddress {
    String host
    int port
}

class ExchangeContext {
    String method
    URI uri
    Headers headers
    InetSocketAddress remoteAddress
    String listenAddr
}

class HeaderParseResult {
    int status
    String contentType
    List<Map.Entry<String, String>> headers
    byte[] body
}

String envOr(String key, String fallback) {
    String value = System.getenv(key)
    value == null || value.isEmpty() ? fallback : value
}

int parseTimeout(String value) {
    try {
        int parsed = Integer.parseInt(value)
        parsed > 0 ? parsed : 30
    } catch (NumberFormatException ignored) {
        30
    }
}

List<String> normalizeRequestPath(String path) {
    String collapsed = path.replaceAll('/+', '/')
    if (collapsed.isEmpty()) {
        collapsed = '/'
    }
    String trimmed = collapsed == '/' ? collapsed : collapsed.replaceAll('/$', '')
    List<String> segs = []
    for (String segment : trimmed.split('/')) {
        if (segment.isEmpty()) {
            continue
        }
        String decoded = URLDecoder.decode(segment, StandardCharsets.UTF_8)
        if (decoded == '..') {
            throw new IllegalArgumentException('invalid path')
        }
        segs << decoded
    }
    segs
}

Node buildTree(String routeDir, Set<String> methods) {
    Path absDir = Paths.get(routeDir).toRealPath()
    Node root = new Node()
    Files.walkFileTree(absDir, EnumSet.of(FileVisitOption.FOLLOW_LINKS), Integer.MAX_VALUE, new FileVisitor<Path>() {
        @Override
        FileVisitResult preVisitDirectory(Path dir, BasicFileAttributes attrs) {
            FileVisitResult.CONTINUE
        }

        @Override
        FileVisitResult visitFile(Path file, BasicFileAttributes attrs) {
            String method = file.fileName.toString().toUpperCase(Locale.ROOT)
            if (!methods.contains(method)) {
                return FileVisitResult.CONTINUE
            }
            Path relative = absDir.relativize(file.parent)
            Node cur = root
            for (Path segPath : relative) {
                String seg = segPath.toString()
                if (seg.startsWith(':')) {
                    if (cur.param == null) {
                        cur.param = new Node(paramName: seg.substring(1))
                    }
                    cur = cur.param
                } else {
                    if (!cur.literal.containsKey(seg)) {
                        cur.literal.put(seg, new Node())
                    }
                    cur = cur.literal.get(seg)
                }
            }
            cur.handlers.put(method, file)
            FileVisitResult.CONTINUE
        }

        @Override
        FileVisitResult visitFileFailed(Path file, IOException exc) {
            System.err.printf('warning: skipping unreadable file %s: %s%n', file, exc.message)
            FileVisitResult.CONTINUE
        }

        @Override
        FileVisitResult postVisitDirectory(Path dir, IOException exc) {
            FileVisitResult.CONTINUE
        }
    })
    root
}

boolean isExecutable(Path path) {
    try {
        Set<PosixFilePermission> perms = Files.getPosixFilePermissions(path)
        perms.contains(PosixFilePermission.OWNER_EXECUTE) || perms.contains(PosixFilePermission.GROUP_EXECUTE) || perms.contains(PosixFilePermission.OTHERS_EXECUTE)
    } catch (UnsupportedOperationException ignored) {
        Files.isExecutable(path)
    }
}

String joinPrefix(String prefix, String seg) {
    prefix.isEmpty() ? seg : prefix + '/' + seg
}

void collectRoutes(Node node, String prefix, List<RouteItem> items) {
    String route = prefix.isEmpty() ? '/' : '/' + prefix
    node.handlers.each { String method, Path path ->
        String tag
        try {
            tag = isExecutable(path) ? 'exec' : 'static'
        } catch (IOException ignored) {
            tag = 'unknown'
        }
        items << new RouteItem(route: route, method: method, path: path, tag: tag)
    }
    node.literal.keySet().sort().each { String seg ->
        collectRoutes(node.literal.get(seg), joinPrefix(prefix, seg), items)
    }
    if (node.param != null) {
        collectRoutes(node.param, joinPrefix(prefix, ':' + node.param.paramName), items)
    }
}

void printRoutes(Node root, String routeDir) {
    System.err.printf('routes from %s:%n', routeDir)
    List<RouteItem> items = []
    collectRoutes(root, '', items)
    items.sort { a, b ->
        int routeCmp = a.route <=> b.route
        routeCmp != 0 ? routeCmp : (a.method <=> b.method)
    }
    items.each { item ->
        System.err.printf('  %-7s %-45s → %s [%s]%n', item.method, item.route, item.path, item.tag)
    }
    System.err.flush()
}

HostPort splitHostPort(String value) {
    if (value == null || value.isEmpty()) {
        return new HostPort(host: '', port: '')
    }
    if (value.startsWith('[') && value.contains(']')) {
        int end = value.indexOf(']')
        String host = value.substring(1, end)
        String rest = value.substring(end + 1)
        if (rest.startsWith(':')) {
            return new HostPort(host: host, port: rest.substring(1))
        }
        return new HostPort(host: host, port: '')
    }
    if (value.count(':') == 1) {
        int idx = value.lastIndexOf(':')
        return new HostPort(host: value.substring(0, idx), port: value.substring(idx + 1))
    }
    new HostPort(host: value, port: '')
}

String envKey(String value) {
    value.toUpperCase(Locale.ROOT).replace('-', '_')
}

String firstHeader(Headers headers, String key) {
    String value = headers.getFirst(key)
    value == null ? '' : value
}

Map<String, String> buildEnv(ExchangeContext context, Map<String, String> params) {
    Map<String, String> env = new LinkedHashMap<>(System.getenv())
    env.put('REQUEST_METHOD', context.method)
    env.put('REQUEST_URI', context.uri.toString())
    env.put('REQUEST_PATH', context.uri.path)
    env.put('QUERY_STRING', context.uri.rawQuery == null ? '' : context.uri.rawQuery)
    env.put('CONTENT_TYPE', firstHeader(context.headers, 'Content-Type'))
    env.put('CONTENT_LENGTH', firstHeader(context.headers, 'Content-Length'))
    env.put('REMOTE_ADDR', context.remoteAddress == null ? '' : context.remoteAddress.address.hostAddress + ':' + context.remoteAddress.port)

    String hostHeader = firstHeader(context.headers, 'Host')
    HostPort hostPort = splitHostPort(hostHeader.isEmpty() ? context.listenAddr : hostHeader)
    env.put('SERVER_NAME', hostPort.host)
    if (!hostPort.port.isEmpty()) {
        env.put('SERVER_PORT', hostPort.port)
    }

    params.each { String key, String value ->
        env.put('PARAM_' + envKey(key), value)
    }

    Set<String> seenQuery = new HashSet<>()
    String rawQuery = context.uri.rawQuery
    if (rawQuery != null && !rawQuery.isEmpty()) {
        rawQuery.split('&').each { String pair ->
            String[] parts = pair.split('=', 2)
            String key = URLDecoder.decode(parts[0], StandardCharsets.UTF_8)
            if (!seenQuery.contains(key)) {
                seenQuery.add(key)
                String value = parts.length > 1 ? URLDecoder.decode(parts[1], StandardCharsets.UTF_8) : ''
                env.put('QUERY_' + envKey(key), value)
            }
        }
    }

    Set<String> seenHeaders = new HashSet<>()
    context.headers.entrySet().each { Map.Entry<String, List<String>> entry ->
        String lower = entry.key.toLowerCase(Locale.ROOT)
        if (!seenHeaders.contains(lower)) {
            seenHeaders.add(lower)
            env.put('HTTP_' + entry.key.toUpperCase(Locale.ROOT).replace('-', '_'), entry.value.isEmpty() ? '' : entry.value.get(0))
        }
    }
    env
}

int exitToStatus(int code) {
    if (code == 0) {
        return 200
    }
    if (code == 1) {
        return 400
    }
    500
}

boolean looksLikeHeader(byte[] raw) {
    for (byte b : raw) {
        if (b == ((byte) ':')) {
            return true
        }
        if (b == ((byte) '\n') || b == ((byte) '\r') || b == ((byte) ' ') || b == ((byte) '\t')) {
            return false
        }
    }
    false
}

Map.Entry<String, String> parseHeaderLine(String line) {
    int idx = line.indexOf(':')
    if (idx <= 0) {
        return null
    }
    String key = line.substring(0, idx)
    for (int i = 0; i < key.length(); i++) {
        char ch = key.charAt(i)
        if (ch <= 32 || ch == 127) {
            return null
        }
    }
    new AbstractMap.SimpleEntry<>(key, line.substring(idx + 1).trim())
}

HeaderParseResult parseCgiHeaders(byte[] raw, int defaultStatus) {
    int status = defaultStatus
    String contentType = 'application/json'
    List<Map.Entry<String, String>> headers = []
    int pos = 0
    boolean sawBlank = false

    while (pos < raw.length) {
        int newline = pos
        while (newline < raw.length && raw[newline] != ((byte) '\n')) {
            newline++
        }
        byte[] line = Arrays.copyOfRange(raw, pos, newline)
        int nextPos = newline < raw.length ? newline + 1 : raw.length
        if (line.length > 0 && line[line.length - 1] == ((byte) '\r')) {
            line = Arrays.copyOfRange(line, 0, line.length - 1)
        }
        if (line.length == 0) {
            sawBlank = true
            pos = nextPos
            break
        }
        String text = new String(line, StandardCharsets.UTF_8)
        Map.Entry<String, String> parsed = parseHeaderLine(text)
        if (parsed == null) {
            return null
        }
        if (parsed.key.equalsIgnoreCase('Status')) {
            String[] parts = parsed.value.split(/\s+/)
            if (parts.length > 0) {
                try {
                    status = Integer.parseInt(parts[0])
                } catch (NumberFormatException ignored) {
                }
            }
        } else if (parsed.key.equalsIgnoreCase('Content-Type')) {
            contentType = parsed.value
        } else {
            headers << parsed
        }
        pos = nextPos
    }

    if (!sawBlank) {
        return null
    }
    new HeaderParseResult(status: status, contentType: contentType, headers: headers, body: Arrays.copyOfRange(raw, pos, raw.length))
}

byte[] readAllBytes(InputStream input) {
    ByteArrayOutputStream out = new ByteArrayOutputStream()
    byte[] buffer = new byte[8192]
    int read
    while ((read = input.read(buffer)) != -1) {
        out.write(buffer, 0, read)
    }
    out.toByteArray()
}

Callable<byte[]> readStream(InputStream input) {
    return { ->
        input.withCloseable { InputStream stream ->
            readAllBytes(stream)
        }
    } as Callable<byte[]>
}

void sendResponse(HttpExchange exchange, String method, int status, byte[] body) {
    if (method == 'HEAD') {
        exchange.sendResponseHeaders(status, -1)
        exchange.close()
        return
    }
    exchange.sendResponseHeaders(status, body.length)
    exchange.responseBody.withCloseable { OutputStream out ->
        out.write(body)
    }
}

int writeJson(HttpExchange exchange, String method, int status, Map payload) {
    byte[] body = JsonOutput.toJson(payload).getBytes(StandardCharsets.UTF_8)
    Headers headers = exchange.responseHeaders
    headers.set('Content-Type', 'application/json')
    headers.set('Content-Length', Integer.toString(body.length))
    sendResponse(exchange, method, status, body)
    status
}

int writeCgiResponse(HttpExchange exchange, String method, byte[] raw, int exitCode) {
    int status = exitToStatus(exitCode)
    String contentType = 'application/json'
    List<Map.Entry<String, String>> headers = []
    byte[] body = raw

    if (raw.length > 0 && looksLikeHeader(raw)) {
        HeaderParseResult parsed = parseCgiHeaders(raw, status)
        if (parsed != null) {
            status = parsed.status
            contentType = parsed.contentType
            headers = parsed.headers
            body = parsed.body
        }
    }

    Headers responseHeaders = exchange.responseHeaders
    headers.each { Map.Entry<String, String> header ->
        responseHeaders.add(header.key, header.value)
    }
    responseHeaders.set('Content-Type', contentType)
    responseHeaders.set('Content-Length', Integer.toString(body.length))
    sendResponse(exchange, method, status, body)
    status
}

int serveFilesystem(HttpExchange exchange, String method, Path routeDir, List<String> segs, String rawPath, int timeoutSeconds, String listenAddr) {
    Path fallback = routeDir
    for (String seg : segs) {
        fallback = fallback.resolve(seg)
    }
    if (!Files.exists(fallback)) {
        return writeJson(exchange, method, 404, [error: 'not_found', path: rawPath])
    }
    if (Files.isRegularFile(fallback)) {
        return serveFallbackFile(exchange, method, fallback, timeoutSeconds, listenAddr)
    }
    if (Files.isDirectory(fallback)) {
        Map preferred = findDirectoryIndex(fallback)
        if (preferred != null) {
            return serveFallbackFile(exchange, method, preferred.path as Path, timeoutSeconds, listenAddr)
        }
        return serveDirListing(exchange, method, fallback, rawPath)
    }
    return writeJson(exchange, method, 404, [error: 'not_found', path: rawPath])
}

int serveFallbackFile(HttpExchange exchange, String method, Path path, int timeoutSeconds, String listenAddr) {
    try {
        if (isExecutable(path)) {
            return executePlainFile(exchange, method, path, timeoutSeconds, listenAddr)
        }
    } catch (IOException err) {
        return writeJson(exchange, method, 500, [error: 'handler_stat_failed', message: err.message])
    }
    return serveStatic(exchange, method, path)
}

Map findDirectoryIndex(Path dirPath) {
    for (String name : ['index.html', 'index.htm']) {
        Path candidate = dirPath.resolve(name)
        if (Files.isRegularFile(candidate)) {
            return [kind: 'static', path: candidate]
        }
    }
    try {
        return Files.list(dirPath)
            .filter { Files.isRegularFile(it) }
            .filter { it.fileName.toString().startsWith('index.') }
            .sorted { a, b -> a.fileName.toString() <=> b.fileName.toString() }
            .filter {
                try {
                    isExecutable(it)
                } catch (IOException ignored) {
                    false
                }
            }
            .findFirst()
            .map { [kind: 'exec', path: it] }
            .orElse(null)
    } catch (IOException ignored) {
        return null
    }
}

int serveDirListing(HttpExchange exchange, String method, Path dirPath, String requestPath) {
    List<Path> entries
    try {
        entries = Files.list(dirPath).sorted { a, b -> a.fileName.toString() <=> b.fileName.toString() }.toList()
    } catch (IOException err) {
        return writeJson(exchange, method, 500, [error: 'dir_listing_failed', message: err.message])
    }
    String title = 'Index of ' + requestPath
    StringBuilder sb = new StringBuilder()
    sb.append('<!DOCTYPE html><html><head><title>').append(title)
      .append('</title></head><body><h1>').append(title).append('</h1><ul>')
    if (requestPath != '/') {
        sb.append('<li><a href="../">../</a></li>')
    }
    for (Path entry : entries) {
        String name = entry.fileName.toString()
        if (Files.isDirectory(entry)) {
            sb.append('<li><a href="').append(name).append('/">').append(name).append('/</a></li>')
        } else {
            sb.append('<li><a href="').append(name).append('">').append(name).append('</a></li>')
        }
    }
    sb.append('</ul></body></html>')
    byte[] body = sb.toString().getBytes(StandardCharsets.UTF_8)
    Headers headers = exchange.responseHeaders
    headers.set('Content-Type', 'text/html; charset=utf-8')
    headers.set('Content-Length', Integer.toString(body.length))
    if (method == 'HEAD') {
        exchange.sendResponseHeaders(200, -1)
        exchange.close()
    } else {
        exchange.sendResponseHeaders(200, body.length)
        exchange.responseBody.withCloseable { OutputStream out -> out.write(body) }
    }
    return 200
}

int serveStatic(HttpExchange exchange, String method, Path handlerPath) {
    try {
        byte[] data = Files.readAllBytes(handlerPath)
        String contentType = Files.probeContentType(handlerPath)
        if (contentType == null || contentType.isBlank()) {
            contentType = 'application/octet-stream'
        }
        Headers headers = exchange.responseHeaders
        headers.set('Content-Type', contentType)
        headers.set('Content-Length', Integer.toString(data.length))
        sendResponse(exchange, method, 200, data)
        return 200
    } catch (IOException err) {
        return writeJson(exchange, method, 500, [error: 'static_read_failed', message: err.message])
    }
}

int handleHandler(HttpExchange exchange, String method, Path handlerPath, Map<String, String> params, int timeoutSeconds, String listenAddr) {
    try {
        if (!isExecutable(handlerPath)) {
            return serveStatic(exchange, method, handlerPath)
        }
    } catch (IOException err) {
        return writeJson(exchange, method, 500, [error: 'handler_stat_failed', message: err.message])
    }

    byte[] requestBody = readAllBytes(exchange.requestBody)
    Map<String, String> env = buildEnv(new ExchangeContext(method: method, uri: exchange.requestURI, headers: exchange.requestHeaders, remoteAddress: exchange.remoteAddress, listenAddr: listenAddr), params)

    Process process
    try {
        ProcessBuilder builder = new ProcessBuilder(handlerPath.toString())
        builder.directory(handlerPath.parent.toFile())
        builder.environment().clear()
        builder.environment().putAll(env)
        process = builder.start()
    } catch (IOException err) {
        return writeJson(exchange, method, 502, [error: 'exec_failed', message: err.message])
    }

    ExecutorService ioExecutor = Executors.newFixedThreadPool(2)
    Future<byte[]> stdoutFuture = ioExecutor.submit(readStream(process.inputStream))
    Future<byte[]> stderrFuture = ioExecutor.submit(readStream(process.errorStream))
    process.outputStream.withCloseable { OutputStream stdin ->
        stdin.write(requestBody)
    }

    byte[] stdout
    byte[] stderr
    int exitCode
    try {
        boolean finished = process.waitFor(timeoutSeconds, TimeUnit.SECONDS)
        if (!finished) {
            process.destroyForcibly()
            process.waitFor(1, TimeUnit.SECONDS)
            stdoutFuture.cancel(true)
            stderrFuture.cancel(true)
            ioExecutor.shutdownNow()
            return writeJson(exchange, method, 504, [error: 'handler_timeout', timeout_seconds: timeoutSeconds])
        }
        exitCode = process.exitValue()
        stdout = stdoutFuture.get()
        stderr = stderrFuture.get()
    } catch (InterruptedException err) {
        Thread.currentThread().interrupt()
        process.destroyForcibly()
        ioExecutor.shutdownNow()
        return writeJson(exchange, method, 500, [error: 'exec_failed', message: err.message])
    } catch (ExecutionException err) {
        process.destroyForcibly()
        ioExecutor.shutdownNow()
        Throwable cause = err.cause == null ? err : err.cause
        return writeJson(exchange, method, 500, [error: 'exec_failed', message: cause.message == null ? cause.toString() : cause.message])
    } finally {
        ioExecutor.shutdownNow()
    }

    if (stderr.length > 0) {
        System.err.printf('  [handler stderr] %s%n', new String(stderr, StandardCharsets.UTF_8).replaceFirst(/[\r\n]+$/, ''))
        System.err.flush()
    }

    byte[] raw = stdout
    if (raw.length == 0 && exitCode != 0 && stderr.length > 0) {
        raw = stderr
    }
    writeCgiResponse(exchange, method, raw, exitCode)
}

int executePlainFile(HttpExchange exchange, String method, Path handlerPath, int timeoutSeconds, String listenAddr) {
    byte[] requestBody = readAllBytes(exchange.requestBody)
    Map<String, String> env = buildEnv(new ExchangeContext(method: method, uri: exchange.requestURI, headers: exchange.requestHeaders, remoteAddress: exchange.remoteAddress, listenAddr: listenAddr), [:])

    Process process
    try {
        ProcessBuilder builder = new ProcessBuilder(handlerPath.toString())
        builder.directory(handlerPath.parent.toFile())
        builder.environment().clear()
        builder.environment().putAll(env)
        process = builder.start()
    } catch (IOException err) {
        return writeJson(exchange, method, 502, [error: 'exec_failed', message: err.message])
    }

    ExecutorService ioExecutor = Executors.newFixedThreadPool(2)
    Future<byte[]> stdoutFuture = ioExecutor.submit(readStream(process.inputStream))
    Future<byte[]> stderrFuture = ioExecutor.submit(readStream(process.errorStream))
    process.outputStream.withCloseable { OutputStream stdin ->
        stdin.write(requestBody)
    }

    byte[] stdout
    byte[] stderr
    int exitCode
    try {
        boolean finished = process.waitFor(timeoutSeconds, TimeUnit.SECONDS)
        if (!finished) {
            process.destroyForcibly()
            process.waitFor(1, TimeUnit.SECONDS)
            stdoutFuture.cancel(true)
            stderrFuture.cancel(true)
            ioExecutor.shutdownNow()
            return writeJson(exchange, method, 504, [error: 'handler_timeout', timeout_seconds: timeoutSeconds])
        }
        exitCode = process.exitValue()
        stdout = stdoutFuture.get()
        stderr = stderrFuture.get()
    } catch (InterruptedException err) {
        Thread.currentThread().interrupt()
        process.destroyForcibly()
        ioExecutor.shutdownNow()
        return writeJson(exchange, method, 500, [error: 'exec_failed', message: err.message])
    } catch (ExecutionException err) {
        process.destroyForcibly()
        ioExecutor.shutdownNow()
        Throwable cause = err.cause == null ? err : err.cause
        return writeJson(exchange, method, 500, [error: 'exec_failed', message: cause.message == null ? cause.toString() : cause.message])
    } finally {
        ioExecutor.shutdownNow()
    }

    if (stderr.length > 0) {
        System.err.printf('  [handler stderr] %s%n', new String(stderr, StandardCharsets.UTF_8).replaceFirst(/[\r\n]+$/, ''))
        System.err.flush()
    }

    Headers headers = exchange.responseHeaders
    headers.set('Content-Type', 'text/plain')
    headers.set('Content-Length', Integer.toString(stdout.length))
    sendResponse(exchange, method, exitToStatus(exitCode), stdout)
    exitToStatus(exitCode)
}

void logResult(String method, String path, int status, long startNanos) {
    double elapsed = Duration.ofNanos(System.nanoTime() - startNanos).toNanos() / 1_000_000_000.0d
    System.err.printf('%s %s → %d (%.6fs)%n', method, path, status, elapsed)
    System.err.flush()
}

class RouterHandler implements HttpHandler {
    Node root
    int timeoutSeconds
    String listenAddr
    Path routeDir
    def support

    @Override
    void handle(HttpExchange exchange) throws IOException {
        long start = System.nanoTime()
        String method = exchange.requestMethod.toUpperCase(Locale.ROOT)
        String rawPath = exchange.requestURI.path
        int status
        try {
            List<String> segs = support.normalizeRequestPath(rawPath)
            Map match = root.match(segs)
            Node node = (Node) match.node
            Map<String, String> params = (Map<String, String>) (match.params ?: [:])
            if (node == null || node.handlers.isEmpty()) {
                status = support.serveFilesystem(exchange, method, routeDir, segs, rawPath, timeoutSeconds, listenAddr)
                support.logResult(method, rawPath, status, start)
                return
            }

            Path handlerPath = node.handlers.get(method)
            if (handlerPath == null && method == 'HEAD') {
                handlerPath = node.handlers.get('GET')
            }
            if (handlerPath == null) {
                List<String> allowed = node.handlers.keySet().toList().sort()
                exchange.responseHeaders.set('Allow', allowed.join(', '))
                status = support.writeJson(exchange, method, 405, [error: 'method_not_allowed', allow: allowed])
                support.logResult(method, rawPath, status, start)
                return
            }
            status = support.handleHandler(exchange, method, handlerPath, params, timeoutSeconds, listenAddr)
        } catch (IllegalArgumentException ignored) {
            status = support.writeJson(exchange, method, 400, [error: 'invalid_path', path: rawPath])
        }
        support.logResult(method, rawPath, status, start)
    }
}

ListenAddress parseListenAddr(String addr) {
    if (addr.startsWith(':')) {
        return new ListenAddress(host: '', port: Integer.parseInt(addr.substring(1)))
    }
    if (addr.startsWith('[') && addr.contains(']')) {
        HostPort hp = splitHostPort(addr)
        return new ListenAddress(host: hp.host, port: Integer.parseInt(hp.port))
    }
    if (addr.count(':') == 1) {
        int idx = addr.lastIndexOf(':')
        return new ListenAddress(host: addr.substring(0, idx), port: Integer.parseInt(addr.substring(idx + 1)))
    }
    new ListenAddress(host: addr, port: 8080)
}

int main() {
    String routeDir = envOr('ROUTE_DIR', './routes')
    String listenAddr = envOr('LISTEN_ADDR', ':8080')
    int timeoutSeconds = parseTimeout(envOr('COMMAND_TIMEOUT', '30'))

    Node root
    try {
        root = buildTree(routeDir, HTTP_METHODS)
    } catch (IOException err) {
        System.err.printf('failed to scan %s: %s%n', routeDir, err.message)
        System.err.flush()
        return 1
    }

    printRoutes(root, routeDir)
    ListenAddress address = parseListenAddr(listenAddr)
    InetSocketAddress bind = address.host.isEmpty() ? new InetSocketAddress(address.port) : new InetSocketAddress(address.host, address.port)
    HttpServer server = HttpServer.create(bind, 0)
    Path routeDirAbs = Paths.get(routeDir).toAbsolutePath().normalize()
    server.createContext('/', new RouterHandler(root: root, timeoutSeconds: timeoutSeconds, listenAddr: listenAddr, routeDir: routeDirAbs, support: this))
    server.executor = Executors.newCachedThreadPool()
    Runtime.runtime.addShutdownHook(new Thread({
        System.err.println('shutting down...')
        System.err.flush()
        server.stop(0)
    }))
    System.err.printf('listening on %s (timeout %ds)%n', listenAddr, timeoutSeconds)
    System.err.flush()
    server.start()
    new CountDownLatch(1).await()
    return 0
}

System.exit(main())
