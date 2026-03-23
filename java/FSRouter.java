import com.sun.net.httpserver.Headers;
import com.sun.net.httpserver.HttpExchange;
import com.sun.net.httpserver.HttpHandler;
import com.sun.net.httpserver.HttpServer;

import java.io.ByteArrayOutputStream;
import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.InetSocketAddress;
import java.net.URI;
import java.net.URLDecoder;
import java.nio.charset.StandardCharsets;
import java.nio.file.FileVisitResult;
import java.nio.file.FileVisitOption;
import java.nio.file.FileVisitor;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.Paths;
import java.nio.file.attribute.BasicFileAttributes;
import java.nio.file.attribute.PosixFilePermission;
import java.time.Duration;
import java.util.ArrayList;
import java.util.Collections;
import java.util.Comparator;
import java.util.EnumSet;
import java.util.HashMap;
import java.util.HashSet;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import java.util.Set;
import java.util.TreeMap;
import java.util.concurrent.Callable;
import java.util.concurrent.ExecutionException;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.Future;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.TimeoutException;

public class FSRouter {
    private static final Set<String> HTTP_METHODS = Set.of("GET", "HEAD", "POST", "PUT", "DELETE", "PATCH", "OPTIONS");

    private static final class Node {
        final Map<String, Node> literal = new TreeMap<>();
        Node param;
        String paramName = "";
        final Map<String, Path> handlers = new TreeMap<>();

        MatchResult match(List<String> segs) {
            Map<String, String> params = new LinkedHashMap<>();
            Node cur = this;
            for (String seg : segs) {
                Node next = cur.literal.get(seg);
                if (next != null) {
                    cur = next;
                    continue;
                }
                if (cur.param != null) {
                    params.put(cur.param.paramName, seg);
                    cur = cur.param;
                    continue;
                }
                return new MatchResult(null, null);
            }
            return new MatchResult(cur, params);
        }
    }

    private record MatchResult(Node node, Map<String, String> params) {}
    private record HeaderParseResult(int status, String contentType, List<Map.Entry<String, String>> headers, byte[] body) {}
    private record RouteItem(String route, String method, Path path, String tag) {}
    private record ListenAddress(String host, int port) {}
    private record HostPort(String host, String port) {}
    private record ExchangeContext(String method, URI uri, Headers headers, InetSocketAddress remoteAddress, String listenAddr) {}

    public static void main(String[] args) throws Exception {
        String routeDir = envOr("ROUTE_DIR", "./routes");
        String listenAddr = envOr("LISTEN_ADDR", ":8080");
        int timeoutSeconds = parseTimeout(envOr("COMMAND_TIMEOUT", "30"));

        Node root;
        try {
            root = buildTree(routeDir);
        } catch (IOException e) {
            System.err.printf("failed to scan %s: %s%n", routeDir, e.getMessage());
            System.err.flush();
            System.exit(1);
            return;
        }

        printRoutes(root, routeDir);
        ListenAddress address = parseListenAddr(listenAddr);
        InetSocketAddress bind = address.host().isEmpty() ? new InetSocketAddress(address.port()) : new InetSocketAddress(address.host(), address.port());
        HttpServer server = HttpServer.create(bind, 0);
        Path routeDirAbs = Paths.get(routeDir).toAbsolutePath().normalize();
        server.createContext("/", new RouterHandler(root, timeoutSeconds, listenAddr, routeDirAbs));
        server.setExecutor(Executors.newCachedThreadPool());
        Runtime.getRuntime().addShutdownHook(new Thread(() -> {
            System.err.println("shutting down...");
            System.err.flush();
            server.stop(0);
        }));
        System.err.printf("listening on %s (timeout %ds)%n", listenAddr, timeoutSeconds);
        System.err.flush();
        server.start();
    }

    private static final class RouterHandler implements HttpHandler {
        private final Node root;
        private final int timeoutSeconds;
        private final String listenAddr;
        private final Path routeDir;

        private RouterHandler(Node root, int timeoutSeconds, String listenAddr, Path routeDir) {
            this.root = root;
            this.timeoutSeconds = timeoutSeconds;
            this.listenAddr = listenAddr;
            this.routeDir = routeDir;
        }

        @Override
        public void handle(HttpExchange exchange) throws IOException {
            long start = System.nanoTime();
            String method = exchange.getRequestMethod().toUpperCase(Locale.ROOT);
            String rawPath = exchange.getRequestURI().getPath();
            int status;
            try {
                List<String> segs = normalizeRequestPath(rawPath);
                MatchResult match = root.match(segs);
                if (match.node() == null || match.node().handlers.isEmpty()) {
                    status = serveFilesystem(exchange, method, routeDir, segs, rawPath, timeoutSeconds, listenAddr);
                    logResult(method, rawPath, status, start);
                    return;
                }

                Path handlerPath = match.node().handlers.get(method);
                if (handlerPath == null && method.equals("HEAD")) {
                    handlerPath = match.node().handlers.get("GET");
                }
                if (handlerPath == null) {
                    List<String> allowed = new ArrayList<>(match.node().handlers.keySet());
                    Collections.sort(allowed);
                    exchange.getResponseHeaders().set("Allow", String.join(", ", allowed));
                    status = writeJson(exchange, method, 405, "{\"error\":\"method_not_allowed\",\"allow\":[" + quoteList(allowed) + "]}");
                    logResult(method, rawPath, status, start);
                    return;
                }

                status = handleHandler(exchange, method, handlerPath, match.params() == null ? Map.of() : match.params(), timeoutSeconds, listenAddr);
            } catch (IllegalArgumentException e) {
                status = writeJson(exchange, method, 400, jsonObject(Map.of("error", "invalid_path", "path", rawPath)));
            }
            logResult(method, rawPath, status, start);
        }
    }

    private static int serveFilesystem(HttpExchange exchange, String method, Path routeDir, List<String> segs, String rawPath, int timeoutSeconds, String listenAddr) throws IOException {
        Path fallback = routeDir;
        for (String seg : segs) {
            fallback = fallback.resolve(seg);
        }
        if (!Files.exists(fallback)) {
            return writeJson(exchange, method, 404, jsonObject(Map.of("error", "not_found", "path", rawPath)));
        }
        if (Files.isRegularFile(fallback)) {
            return serveStatic(exchange, method, fallback);
        }
        if (Files.isDirectory(fallback)) {
            DirectoryIndex preferred = findDirectoryIndex(fallback);
            if (preferred != null) {
                if (preferred.kind.equals("static")) {
                    return serveStatic(exchange, method, preferred.path);
                }
                return handleHandler(exchange, method, preferred.path, Map.of(), timeoutSeconds, listenAddr);
            }
            return serveDirListing(exchange, method, fallback, rawPath);
        }
        return writeJson(exchange, method, 404, jsonObject(Map.of("error", "not_found", "path", rawPath)));
    }

    private record DirectoryIndex(String kind, Path path) {}

    private static DirectoryIndex findDirectoryIndex(Path dirPath) {
        for (String name : List.of("index.html", "index.htm")) {
            Path candidate = dirPath.resolve(name);
            if (Files.isRegularFile(candidate)) {
                return new DirectoryIndex("static", candidate);
            }
        }
        try (var stream = Files.list(dirPath)) {
            return stream
                .filter(Files::isRegularFile)
                .filter(path -> path.getFileName().toString().startsWith("index."))
                .sorted(Comparator.comparing(path -> path.getFileName().toString()))
                .filter(path -> {
                    try {
                        return isExecutable(path);
                    } catch (IOException e) {
                        return false;
                    }
                })
                .findFirst()
                .map(path -> new DirectoryIndex("exec", path))
                .orElse(null);
        } catch (IOException e) {
            return null;
        }
    }

    private static int serveDirListing(HttpExchange exchange, String method, Path dirPath, String requestPath) throws IOException {
        List<Path> entries;
        try (var stream = Files.list(dirPath)) {
            entries = stream.sorted((a, b) -> a.getFileName().toString().compareTo(b.getFileName().toString())).toList();
        } catch (IOException e) {
            return writeJson(exchange, method, 500, jsonObject(Map.of("error", "dir_listing_failed", "message", e.getMessage())));
        }
        String title = "Index of " + requestPath;
        StringBuilder sb = new StringBuilder();
        sb.append("<!DOCTYPE html><html><head><title>").append(title)
          .append("</title></head><body><h1>").append(title).append("</h1><ul>");
        if (!requestPath.equals("/")) {
            sb.append("<li><a href=\"../\">../</a></li>");
        }
        for (Path entry : entries) {
            String name = entry.getFileName().toString();
            if (Files.isDirectory(entry)) {
                sb.append("<li><a href=\"").append(name).append("/\">").append(name).append("/</a></li>");
            } else {
                sb.append("<li><a href=\"").append(name).append("\">").append(name).append("</a></li>");
            }
        }
        sb.append("</ul></body></html>");
        byte[] body = sb.toString().getBytes(StandardCharsets.UTF_8);
        Headers headers = exchange.getResponseHeaders();
        headers.set("Content-Type", "text/html; charset=utf-8");
        headers.set("Content-Length", Integer.toString(body.length));
        exchange.sendResponseHeaders(200, method.equals("HEAD") ? -1 : body.length);
        if (!method.equals("HEAD")) {
            try (OutputStream out = exchange.getResponseBody()) {
                out.write(body);
            }
        } else {
            exchange.close();
        }
        return 200;
    }

    private static int handleHandler(HttpExchange exchange, String method, Path handlerPath, Map<String, String> params, int timeoutSeconds, String listenAddr) throws IOException {
        try {
            if (!isExecutable(handlerPath)) {
                return serveStatic(exchange, method, handlerPath);
            }
        } catch (IOException e) {
            return writeJson(exchange, method, 500, jsonObject(Map.of("error", "handler_stat_failed", "message", e.getMessage())));
        }

        byte[] requestBody = readAllBytes(exchange.getRequestBody());
        ExchangeContext context = new ExchangeContext(method, exchange.getRequestURI(), exchange.getRequestHeaders(), exchange.getRemoteAddress(), listenAddr);
        Map<String, String> env = buildEnv(context, params);
        ProcessBuilder builder = new ProcessBuilder(handlerPath.toString());
        builder.directory(handlerPath.getParent().toFile());
        builder.environment().clear();
        builder.environment().putAll(env);

        Process process;
        try {
            process = builder.start();
        } catch (IOException e) {
            return writeJson(exchange, method, 502, jsonObject(Map.of("error", "exec_failed", "message", e.getMessage())));
        }

        ExecutorService ioExecutor = Executors.newFixedThreadPool(2);
        Future<byte[]> stdoutFuture = ioExecutor.submit(readStream(process.getInputStream()));
        Future<byte[]> stderrFuture = ioExecutor.submit(readStream(process.getErrorStream()));
        try (OutputStream stdin = process.getOutputStream()) {
            stdin.write(requestBody);
        }

        byte[] stdout;
        byte[] stderr;
        int exitCode;
        try {
            boolean finished = process.waitFor(timeoutSeconds, TimeUnit.SECONDS);
            if (!finished) {
                process.destroyForcibly();
                process.waitFor(1, TimeUnit.SECONDS);
                stdoutFuture.cancel(true);
                stderrFuture.cancel(true);
                ioExecutor.shutdownNow();
                return writeJson(exchange, method, 504, "{\"error\":\"handler_timeout\",\"timeout_seconds\":" + timeoutSeconds + "}");
            }
            exitCode = process.exitValue();
            stdout = stdoutFuture.get();
            stderr = stderrFuture.get();
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
            process.destroyForcibly();
            ioExecutor.shutdownNow();
            return writeJson(exchange, method, 500, jsonObject(Map.of("error", "exec_failed", "message", e.getMessage())));
        } catch (ExecutionException e) {
            process.destroyForcibly();
            ioExecutor.shutdownNow();
            Throwable cause = e.getCause() == null ? e : e.getCause();
            return writeJson(exchange, method, 500, jsonObject(Map.of("error", "exec_failed", "message", cause.getMessage() == null ? cause.toString() : cause.getMessage())));
        } finally {
            ioExecutor.shutdownNow();
        }

        if (stderr.length > 0) {
            System.err.printf("  [handler stderr] %s%n", new String(stderr, StandardCharsets.UTF_8).stripTrailing());
            System.err.flush();
        }

        byte[] raw = stdout;
        if (raw.length == 0 && exitCode != 0 && stderr.length > 0) {
            raw = stderr;
        }
        return writeCgiResponse(exchange, method, raw, exitCode);
    }

    private static int serveStatic(HttpExchange exchange, String method, Path handlerPath) throws IOException {
        byte[] data;
        try {
            data = Files.readAllBytes(handlerPath);
        } catch (IOException e) {
            return writeJson(exchange, method, 500, jsonObject(Map.of("error", "static_read_failed", "message", e.getMessage())));
        }
        String contentType = Files.probeContentType(handlerPath);
        if (contentType == null || contentType.isBlank()) {
            contentType = "application/octet-stream";
        }
        Headers headers = exchange.getResponseHeaders();
        headers.set("Content-Type", contentType);
        headers.set("Content-Length", Integer.toString(data.length));
        sendResponse(exchange, method, 200, data);
        return 200;
    }

    private static int writeCgiResponse(HttpExchange exchange, String method, byte[] raw, int exitCode) throws IOException {
        int status = exitToStatus(exitCode);
        String contentType = "application/json";
        List<Map.Entry<String, String>> headers = new ArrayList<>();
        byte[] body = raw;

        if (raw.length > 0 && looksLikeHeader(raw)) {
            HeaderParseResult parsed = parseCgiHeaders(raw, status);
            if (parsed != null) {
                status = parsed.status();
                contentType = parsed.contentType();
                headers = parsed.headers();
                body = parsed.body();
            }
        }

        Headers responseHeaders = exchange.getResponseHeaders();
        for (Map.Entry<String, String> header : headers) {
            responseHeaders.add(header.getKey(), header.getValue());
        }
        responseHeaders.set("Content-Type", contentType);
        responseHeaders.set("Content-Length", Integer.toString(body.length));
        sendResponse(exchange, method, status, body);
        return status;
    }

    private static int writeJson(HttpExchange exchange, String method, int status, String payload) throws IOException {
        byte[] body = payload.getBytes(StandardCharsets.UTF_8);
        Headers headers = exchange.getResponseHeaders();
        headers.set("Content-Type", "application/json");
        headers.set("Content-Length", Integer.toString(body.length));
        sendResponse(exchange, method, status, body);
        return status;
    }

    private static void sendResponse(HttpExchange exchange, String method, int status, byte[] body) throws IOException {
        if (method.equals("HEAD")) {
            exchange.sendResponseHeaders(status, -1);
            exchange.close();
            return;
        }
        exchange.sendResponseHeaders(status, body.length);
        try (OutputStream out = exchange.getResponseBody()) {
            out.write(body);
        }
    }

    private static List<String> normalizeRequestPath(String path) {
        String collapsed = path.replaceAll("/+", "/");
        if (collapsed.isEmpty()) {
            collapsed = "/";
        }
        String trimmed = collapsed.equals("/") ? collapsed : collapsed.replaceAll("/$", "");
        List<String> segs = new ArrayList<>();
        for (String segment : trimmed.split("/")) {
            if (segment.isEmpty()) {
                continue;
            }
            String decoded = URLDecoder.decode(segment, StandardCharsets.UTF_8);
            if (decoded.equals("..")) {
                throw new IllegalArgumentException("invalid path");
            }
            segs.add(decoded);
        }
        return segs;
    }

    private static Node buildTree(String routeDir) throws IOException {
        Path absDir = Paths.get(routeDir).toRealPath();
        Node root = new Node();
        Files.walkFileTree(absDir, EnumSet.of(FileVisitOption.FOLLOW_LINKS), Integer.MAX_VALUE, new FileVisitor<>() {
            @Override
            public FileVisitResult preVisitDirectory(Path dir, BasicFileAttributes attrs) {
                return FileVisitResult.CONTINUE;
            }

            @Override
            public FileVisitResult visitFile(Path file, BasicFileAttributes attrs) {
                String method = file.getFileName().toString().toUpperCase(Locale.ROOT);
                if (!HTTP_METHODS.contains(method)) {
                    return FileVisitResult.CONTINUE;
                }
                Path relative = absDir.relativize(file.getParent());
                Node cur = root;
                for (Path segPath : relative) {
                    String seg = segPath.toString();
                    if (seg.startsWith(":")) {
                        if (cur.param == null) {
                            cur.param = new Node();
                            cur.param.paramName = seg.substring(1);
                        }
                        cur = cur.param;
                    } else {
                        cur.literal.putIfAbsent(seg, new Node());
                        cur = cur.literal.get(seg);
                    }
                }
                cur.handlers.put(method, file);
                return FileVisitResult.CONTINUE;
            }

            @Override
            public FileVisitResult visitFileFailed(Path file, IOException exc) {
                System.err.printf("warning: skipping unreadable file %s: %s%n", file, exc.getMessage());
                return FileVisitResult.CONTINUE;
            }

            @Override
            public FileVisitResult postVisitDirectory(Path dir, IOException exc) {
                return FileVisitResult.CONTINUE;
            }
        });
        return root;
    }

    private static void collectRoutes(Node node, String prefix, List<RouteItem> items) {
        String route = prefix.isEmpty() ? "/" : "/" + prefix;
        for (Map.Entry<String, Path> entry : node.handlers.entrySet()) {
            String tag;
            try {
                tag = isExecutable(entry.getValue()) ? "exec" : "static";
            } catch (IOException e) {
                tag = "unknown";
            }
            items.add(new RouteItem(route, entry.getKey(), entry.getValue(), tag));
        }
        for (String seg : node.literal.keySet()) {
            collectRoutes(node.literal.get(seg), joinPrefix(prefix, seg), items);
        }
        if (node.param != null) {
            collectRoutes(node.param, joinPrefix(prefix, ":" + node.param.paramName), items);
        }
    }

    private static String joinPrefix(String prefix, String seg) {
        return prefix.isEmpty() ? seg : prefix + "/" + seg;
    }

    private static void printRoutes(Node root, String routeDir) {
        System.err.printf("routes from %s:%n", routeDir);
        List<RouteItem> items = new ArrayList<>();
        collectRoutes(root, "", items);
        items.sort((a, b) -> {
            int routeCmp = a.route().compareTo(b.route());
            if (routeCmp != 0) {
                return routeCmp;
            }
            return a.method().compareTo(b.method());
        });
        for (RouteItem item : items) {
            System.err.printf("  %-7s %-45s → %s [%s]%n", item.method(), item.route(), item.path(), item.tag());
        }
        System.err.flush();
    }

    private static Map<String, String> buildEnv(ExchangeContext context, Map<String, String> params) {
        Map<String, String> env = new HashMap<>(System.getenv());
        env.put("REQUEST_METHOD", context.method());
        env.put("REQUEST_URI", context.uri().toString());
        env.put("REQUEST_PATH", context.uri().getPath());
        env.put("QUERY_STRING", context.uri().getRawQuery() == null ? "" : context.uri().getRawQuery());
        env.put("CONTENT_TYPE", firstHeader(context.headers(), "Content-Type"));
        env.put("CONTENT_LENGTH", firstHeader(context.headers(), "Content-Length"));
        env.put("REMOTE_ADDR", context.remoteAddress().getAddress().getHostAddress() + ":" + context.remoteAddress().getPort());

        String hostHeader = firstHeader(context.headers(), "Host");
        HostPort hostPort = splitHostPort(hostHeader.isEmpty() ? context.listenAddr() : hostHeader);
        env.put("SERVER_NAME", hostPort.host());
        if (!hostPort.port().isEmpty()) {
            env.put("SERVER_PORT", hostPort.port());
        }

        for (Map.Entry<String, String> entry : params.entrySet()) {
            env.put("PARAM_" + envKey(entry.getKey()), entry.getValue());
        }

        Set<String> seenQuery = new HashSet<>();
        String rawQuery = context.uri().getRawQuery();
        if (rawQuery != null && !rawQuery.isEmpty()) {
            for (String pair : rawQuery.split("&")) {
                String[] parts = pair.split("=", 2);
                String key = URLDecoder.decode(parts[0], StandardCharsets.UTF_8);
                if (seenQuery.contains(key)) {
                    continue;
                }
                seenQuery.add(key);
                String value = parts.length > 1 ? URLDecoder.decode(parts[1], StandardCharsets.UTF_8) : "";
                env.put("QUERY_" + envKey(key), value);
            }
        }

        Set<String> seenHeaders = new HashSet<>();
        for (Map.Entry<String, List<String>> entry : context.headers().entrySet()) {
            String lower = entry.getKey().toLowerCase(Locale.ROOT);
            if (seenHeaders.contains(lower)) {
                continue;
            }
            seenHeaders.add(lower);
            env.put("HTTP_" + entry.getKey().toUpperCase(Locale.ROOT).replace('-', '_'), entry.getValue().isEmpty() ? "" : entry.getValue().get(0));
        }
        return env;
    }

    private static HostPort splitHostPort(String value) {
        if (value == null || value.isEmpty()) {
            return new HostPort("", "");
        }
        if (value.startsWith("[") && value.contains("]")) {
            int end = value.indexOf(']');
            String host = value.substring(1, end);
            String rest = value.substring(end + 1);
            if (rest.startsWith(":")) {
                return new HostPort(host, rest.substring(1));
            }
            return new HostPort(host, "");
        }
        if (value.chars().filter(ch -> ch == ':').count() == 1) {
            int idx = value.lastIndexOf(':');
            return new HostPort(value.substring(0, idx), value.substring(idx + 1));
        }
        return new HostPort(value, "");
    }

    private static String envKey(String value) {
        return value.toUpperCase(Locale.ROOT).replace('-', '_');
    }

    private static HeaderParseResult parseCgiHeaders(byte[] raw, int defaultStatus) {
        int status = defaultStatus;
        String contentType = "application/json";
        List<Map.Entry<String, String>> headers = new ArrayList<>();
        int pos = 0;
        boolean sawBlank = false;

        while (pos < raw.length) {
            int newline = indexOf(raw, (byte) '\n', pos);
            byte[] line;
            int nextPos;
            if (newline == -1) {
                line = slice(raw, pos, raw.length);
                nextPos = raw.length;
            } else {
                line = slice(raw, pos, newline);
                nextPos = newline + 1;
            }
            if (line.length > 0 && line[line.length - 1] == '\r') {
                line = slice(line, 0, line.length - 1);
            }
            if (line.length == 0) {
                sawBlank = true;
                pos = nextPos;
                break;
            }
            String text = new String(line, StandardCharsets.UTF_8);
            Map.Entry<String, String> parsed = parseHeaderLine(text);
            if (parsed == null) {
                return null;
            }
            String key = parsed.getKey();
            String value = parsed.getValue();
            if (key.equalsIgnoreCase("Status")) {
                String[] parts = value.split("\\s+");
                if (parts.length > 0) {
                    try {
                        status = Integer.parseInt(parts[0]);
                    } catch (NumberFormatException ignored) {
                    }
                }
            } else if (key.equalsIgnoreCase("Content-Type")) {
                contentType = value;
            } else {
                headers.add(Map.entry(key, value));
            }
            pos = nextPos;
        }

        if (!sawBlank) {
            return null;
        }
        return new HeaderParseResult(status, contentType, headers, slice(raw, pos, raw.length));
    }

    private static Map.Entry<String, String> parseHeaderLine(String line) {
        int idx = line.indexOf(':');
        if (idx <= 0) {
            return null;
        }
        String key = line.substring(0, idx);
        for (int i = 0; i < key.length(); i++) {
            char ch = key.charAt(i);
            if (ch <= 32 || ch == 127) {
                return null;
            }
        }
        return Map.entry(key, line.substring(idx + 1).trim());
    }

    private static boolean looksLikeHeader(byte[] raw) {
        for (int i = 0; i < raw.length; i++) {
            byte b = raw[i];
            if (b == ':' && i > 0) {
                return true;
            }
            if (b == '\n' || b == '\r' || b == ' ' || b == '\t') {
                return false;
            }
        }
        return false;
    }

    private static int exitToStatus(int code) {
        if (code == 0) {
            return 200;
        }
        if (code == 1) {
            return 400;
        }
        return 500;
    }

    private static boolean isExecutable(Path path) throws IOException {
        try {
            Set<PosixFilePermission> perms = Files.getPosixFilePermissions(path);
            return perms.contains(PosixFilePermission.OWNER_EXECUTE)
                || perms.contains(PosixFilePermission.GROUP_EXECUTE)
                || perms.contains(PosixFilePermission.OTHERS_EXECUTE);
        } catch (UnsupportedOperationException e) {
            return Files.isExecutable(path);
        }
    }

    private static String envOr(String key, String fallback) {
        String value = System.getenv(key);
        return value == null || value.isEmpty() ? fallback : value;
    }

    private static int parseTimeout(String value) {
        try {
            int parsed = Integer.parseInt(value);
            return parsed > 0 ? parsed : 30;
        } catch (NumberFormatException e) {
            return 30;
        }
    }

    private static ListenAddress parseListenAddr(String addr) {
        if (addr.startsWith(":")) {
            return new ListenAddress("", Integer.parseInt(addr.substring(1)));
        }
        if (addr.startsWith("[") && addr.contains("]")) {
            HostPort hp = splitHostPort(addr);
            return new ListenAddress(hp.host(), Integer.parseInt(hp.port()));
        }
        if (addr.chars().filter(ch -> ch == ':').count() == 1) {
            int idx = addr.lastIndexOf(':');
            return new ListenAddress(addr.substring(0, idx), Integer.parseInt(addr.substring(idx + 1)));
        }
        return new ListenAddress(addr, 8080);
    }

    private static byte[] readAllBytes(InputStream in) throws IOException {
        ByteArrayOutputStream out = new ByteArrayOutputStream();
        byte[] buffer = new byte[8192];
        int read;
        while ((read = in.read(buffer)) != -1) {
            out.write(buffer, 0, read);
        }
        return out.toByteArray();
    }

    private static Callable<byte[]> readStream(InputStream in) {
        return () -> {
            try (InputStream input = in) {
                return readAllBytes(input);
            }
        };
    }

    private static String firstHeader(Headers headers, String key) {
        String value = headers.getFirst(key);
        return value == null ? "" : value;
    }

    private static void logResult(String method, String path, int status, long startNanos) {
        double elapsed = Duration.ofNanos(System.nanoTime() - startNanos).toNanos() / 1_000_000_000.0;
        System.err.printf("%s %s → %d (%.6fs)%n", method, path, status, elapsed);
        System.err.flush();
    }

    private static String jsonObject(Map<String, Object> values) {
        StringBuilder sb = new StringBuilder();
        sb.append('{');
        boolean first = true;
        for (Map.Entry<String, Object> entry : values.entrySet()) {
            if (!first) {
                sb.append(',');
            }
            first = false;
            sb.append(quoteJson(entry.getKey())).append(':').append(jsonValue(entry.getValue()));
        }
        sb.append('}');
        return sb.toString();
    }

    private static String jsonValue(Object value) {
        if (value == null) {
            return "null";
        }
        if (value instanceof String s) {
            return quoteJson(s);
        }
        if (value instanceof Number || value instanceof Boolean) {
            return value.toString();
        }
        if (value instanceof List<?> list) {
            List<String> parts = new ArrayList<>();
            for (Object item : list) {
                parts.add(jsonValue(item));
            }
            return "[" + String.join(",", parts) + "]";
        }
        return quoteJson(value.toString());
    }

    private static String quoteList(List<String> items) {
        List<String> quoted = new ArrayList<>();
        for (String item : items) {
            quoted.add(quoteJson(item));
        }
        return String.join(",", quoted);
    }

    private static String quoteJson(String value) {
        StringBuilder sb = new StringBuilder();
        sb.append('"');
        for (int i = 0; i < value.length(); i++) {
            char ch = value.charAt(i);
            switch (ch) {
                case '\\' -> sb.append("\\\\");
                case '"' -> sb.append("\\\"");
                case '\b' -> sb.append("\\b");
                case '\f' -> sb.append("\\f");
                case '\n' -> sb.append("\\n");
                case '\r' -> sb.append("\\r");
                case '\t' -> sb.append("\\t");
                default -> {
                    if (ch < 0x20) {
                        sb.append(String.format("\\u%04x", (int) ch));
                    } else {
                        sb.append(ch);
                    }
                }
            }
        }
        sb.append('"');
        return sb.toString();
    }

    private static int indexOf(byte[] data, byte target, int start) {
        for (int i = start; i < data.length; i++) {
            if (data[i] == target) {
                return i;
            }
        }
        return -1;
    }

    private static byte[] slice(byte[] data, int start, int end) {
        int len = Math.max(0, end - start);
        byte[] out = new byte[len];
        System.arraycopy(data, start, out, 0, len);
        return out;
    }
}
