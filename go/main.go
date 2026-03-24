// fsrouter — a generic HTTP server whose routes, methods, and handlers
// are defined entirely by a directory tree.
//
// Directory layout:
//
//	routes/
//	  health/
//	    GET                          # executable → runs as handler
//	  api/v1/
//	    widgets/
//	      GET                        # list widgets
//	      POST                       # create widget
//	      :id/
//	        GET                      # get one widget (path param)
//	        DELETE                   # delete widget
//
// Conventions:
//   - Files named GET, POST, PUT, DELETE, PATCH, HEAD, OPTIONS are handlers.
//   - Executable handlers are invoked as subprocesses (CGI-style).
//   - Non-executable handler files are served as static content.
//   - Directories starting with ":" are path parameters.
//   - Literal path segments take priority over parameter segments.
//
// Handler subprocess protocol:
//   - stdin:   request body
//   - stdout:  response (optional CGI headers + body, see below)
//   - stderr:  logged server-side; used as response body on error if stdout is empty
//   - cwd:     set to the handler file's parent directory
//   - env:     inherits server env plus CGI-like variables (see buildEnv)
//
// CGI-style response headers (optional):
//   If the handler's stdout starts with lines that look like HTTP headers
//   ("Key: Value\n") followed by a blank line, they are parsed:
//     Status: 201         → sets HTTP status code
//     Content-Type: ...   → sets content type
//     X-Custom: ...       → set as response header
//   If no headers are detected, the entire stdout is the response body.
//
// Exit code mapping (when no Status header is set):
//   0 → 200    1 → 400 (client error)    other → 500
//
// Environment variables for configuration:
//   ROUTE_DIR        path to routes directory  (default: ./routes)
//   LISTEN_ADDR      bind address              (default: :8080)
//   COMMAND_TIMEOUT   handler timeout seconds   (default: 30)

package main

import (
	"bufio"
	"bytes"
	"context"
	"fmt"
	"log"
	"net"
	"net/http"
	"os"
	"os/exec"
	"os/signal"
	"path/filepath"
	"sort"
	"strconv"
	"strings"
	"syscall"
	"time"
)

// ---------------------------------------------------------------------------
// Route trie
// ---------------------------------------------------------------------------

var httpMethods = map[string]bool{
	"GET": true, "HEAD": true, "POST": true, "PUT": true,
	"DELETE": true, "PATCH": true, "OPTIONS": true,
}

type node struct {
	literal   map[string]*node  // exact-match children
	param     *node             // single wildcard child (:name)
	paramName string            // name without leading colon
	handlers  map[string]string // METHOD → absolute file path
}

func newNode() *node {
	return &node{
		literal:  make(map[string]*node),
		handlers: make(map[string]string),
	}
}

// match resolves URL path segments against the trie.
// Literal children are checked before the param child at each level.
func (n *node) match(segs []string) (*node, map[string]string) {
	params := map[string]string{}
	cur := n
	for _, s := range segs {
		if child, ok := cur.literal[s]; ok {
			cur = child
		} else if cur.param != nil {
			params[cur.param.paramName] = s
			cur = cur.param
		} else {
			return nil, nil
		}
	}
	return cur, params
}

// buildTree walks routeDir and registers every method file it finds.
func buildTree(root *node, routeDir string) error {
	abs, err := filepath.Abs(routeDir)
	if err != nil {
		return err
	}
	return filepath.Walk(abs, func(path string, info os.FileInfo, err error) error {
		if err != nil || info.IsDir() {
			return err
		}
		method := strings.ToUpper(info.Name())
		if !httpMethods[method] {
			return nil
		}

		rel, _ := filepath.Rel(abs, filepath.Dir(path))
		segs := splitSegments(filepath.ToSlash(rel))

		cur := root
		for _, seg := range segs {
			if strings.HasPrefix(seg, ":") {
				if cur.param == nil {
					cur.param = newNode()
					cur.param.paramName = seg[1:]
				}
				cur = cur.param
			} else {
				if _, ok := cur.literal[seg]; !ok {
					cur.literal[seg] = newNode()
				}
				cur = cur.literal[seg]
			}
		}
		cur.handlers[method] = path
		return nil
	})
}

func splitSegments(p string) []string {
	p = strings.Trim(p, "/")
	if p == "" || p == "." {
		return nil
	}
	return strings.Split(p, "/")
}

// printRoutes logs the discovered route table at startup.
func printRoutes(n *node, prefix string) {
	for method, path := range n.handlers {
		tag := "exec"
		if info, err := os.Stat(path); err == nil && info.Mode()&0111 == 0 {
			tag = "static"
		}
		route := "/" + prefix
		if route == "//" {
			route = "/"
		}
		log.Printf("  %-7s %-45s → %s [%s]", method, route, path, tag)
	}
	keys := make([]string, 0, len(n.literal))
	for k := range n.literal {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	for _, k := range keys {
		printRoutes(n.literal[k], joinPrefix(prefix, k))
	}
	if n.param != nil {
		printRoutes(n.param, joinPrefix(prefix, ":"+n.param.paramName))
	}
}

func joinPrefix(prefix, seg string) string {
	if prefix == "" {
		return seg
	}
	return prefix + "/" + seg
}

// ---------------------------------------------------------------------------
// HTTP server
// ---------------------------------------------------------------------------

type server struct {
	root     *node
	timeout  time.Duration
	routeDir string
}

func (s *server) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	start := time.Now()
	segs := splitSegments(r.URL.Path)

	nd, params := s.root.match(segs)
	if nd == nil || len(nd.handlers) == 0 {
		status := s.serveFilesystem(w, r, segs)
		log.Printf("%s %s → %d (%v)", r.Method, r.URL.Path, status, time.Since(start))
		return
	}

	handlerPath, ok := nd.handlers[r.Method]
	if !ok {
		allowed := sortedKeys(nd.handlers)
		w.Header().Set("Allow", strings.Join(allowed, ", "))
		writeJSON(w, 405, `{"error":"method_not_allowed","allow":[%s]}`,
			`"`+strings.Join(allowed, `","`)+`"`)
		log.Printf("%s %s → 405 (%v)", r.Method, r.URL.Path, time.Since(start))
		return
	}

	status := s.handle(w, r, handlerPath, params)
	log.Printf("%s %s → %d (%v)", r.Method, r.URL.Path, status, time.Since(start))
}

func (s *server) serveFilesystem(w http.ResponseWriter, r *http.Request, segs []string) int {
	parts := make([]string, 0, len(segs)+1)
	parts = append(parts, s.routeDir)
	parts = append(parts, segs...)
	fullPath := filepath.Join(parts...)

	info, err := os.Stat(fullPath)
	if err != nil {
		writeJSON(w, 404, `{"error":"not_found","path":%q}`, r.URL.Path)
		return 404
	}
	if !info.IsDir() {
		return s.serveFallbackFile(w, r, fullPath)
	}
	if kind, indexPath, ok := findDirectoryIndex(fullPath); ok {
		_ = kind
		return s.serveFallbackFile(w, r, indexPath)
	}
	return s.serveDirListing(w, r, fullPath)
}

func (s *server) serveFallbackFile(w http.ResponseWriter, r *http.Request, path string) int {
	info, err := os.Stat(path)
	if err != nil {
		writeJSON(w, 500, `{"error":"handler_stat_failed","message":%q}`, err.Error())
		return 500
	}
	if info.Mode()&0111 != 0 {
		return s.executePlainFile(w, r, path)
	}
	http.ServeFile(w, r, path)
	return 200
}

func (s *server) serveDirListing(w http.ResponseWriter, r *http.Request, dirPath string) int {
	entries, err := os.ReadDir(dirPath)
	if err != nil {
		writeJSON(w, 500, `{"error":"dir_listing_failed","message":%q}`, err.Error())
		return 500
	}
	title := "Index of " + r.URL.Path
	var sb strings.Builder
	sb.WriteString("<!DOCTYPE html><html><head><title>")
	sb.WriteString(title)
	sb.WriteString("</title></head><body><h1>")
	sb.WriteString(title)
	sb.WriteString("</h1><ul>")
	if r.URL.Path != "/" {
		sb.WriteString(`<li><a href="../">../</a></li>`)
	}
	for _, entry := range entries {
		name := entry.Name()
		if entry.IsDir() {
			fmt.Fprintf(&sb, `<li><a href="%s/">%s/</a></li>`, name, name)
		} else {
			fmt.Fprintf(&sb, `<li><a href="%s">%s</a></li>`, name, name)
		}
	}
	sb.WriteString("</ul></body></html>")
	body := sb.String()
	w.Header().Set("Content-Type", "text/html; charset=utf-8")
	w.Header().Set("Content-Length", strconv.Itoa(len(body)))
	w.WriteHeader(200)
	if r.Method != "HEAD" {
		fmt.Fprint(w, body)
	}
	return 200
}

func (s *server) handle(w http.ResponseWriter, r *http.Request, path string, params map[string]string) int {
	info, err := os.Stat(path)
	if err != nil {
		writeJSON(w, 500, `{"error":"handler_stat_failed","message":%q}`, err.Error())
		return 500
	}

	// Non-executable file: serve as static content.
	if info.Mode()&0111 == 0 {
		http.ServeFile(w, r, path)
		return 200
	}

	// Executable: run as subprocess.
	ctx, cancel := context.WithTimeout(r.Context(), s.timeout)
	defer cancel()

	cmd := exec.CommandContext(ctx, path)
	cmd.Dir = filepath.Dir(path)
	cmd.Stdin = r.Body
	cmd.Env = buildEnv(r, params)

	var stdout, stderr bytes.Buffer
	cmd.Stdout = &stdout
	cmd.Stderr = &stderr

	runErr := cmd.Run()

	exitCode := 0
	if runErr != nil {
		if ctx.Err() == context.DeadlineExceeded {
			writeJSON(w, 504, `{"error":"handler_timeout","timeout_seconds":%d}`,
				int(s.timeout.Seconds()))
			return 504
		}
		if ee, ok := runErr.(*exec.ExitError); ok {
			exitCode = ee.ExitCode()
		} else {
			writeJSON(w, 502, `{"error":"exec_failed","message":%q}`, runErr.Error())
			return 502
		}
	}

	// Always log stderr output from the handler.
	if stderr.Len() > 0 {
		log.Printf("  [handler stderr] %s", strings.TrimRight(stderr.String(), "\n"))
	}

	// If the handler produced no stdout on failure, fall back to stderr.
	body := stdout.Bytes()
	if len(body) == 0 && exitCode != 0 && stderr.Len() > 0 {
		body = stderr.Bytes()
	}

	return writeCGIResponse(w, body, exitCode)
}

func (s *server) executePlainFile(w http.ResponseWriter, r *http.Request, path string) int {
	ctx, cancel := context.WithTimeout(r.Context(), s.timeout)
	defer cancel()

	cmd := exec.CommandContext(ctx, path)
	cmd.Dir = filepath.Dir(path)
	cmd.Stdin = r.Body
	cmd.Env = buildEnv(r, map[string]string{})

	var stdout, stderr bytes.Buffer
	cmd.Stdout = &stdout
	cmd.Stderr = &stderr

	runErr := cmd.Run()

	exitCode := 0
	if runErr != nil {
		if ctx.Err() == context.DeadlineExceeded {
			writeJSON(w, 504, `{"error":"handler_timeout","timeout_seconds":%d}`,
				int(s.timeout.Seconds()))
			return 504
		}
		if ee, ok := runErr.(*exec.ExitError); ok {
			exitCode = ee.ExitCode()
		} else {
			writeJSON(w, 502, `{"error":"exec_failed","message":%q}`, runErr.Error())
			return 502
		}
	}

	if stderr.Len() > 0 {
		log.Printf("  [handler stderr] %s", strings.TrimRight(stderr.String(), "\n"))
	}

	body := stdout.Bytes()
	status := exitToStatus(exitCode)
	w.Header().Set("Content-Type", "text/plain")
	w.Header().Set("Content-Length", strconv.Itoa(len(body)))
	w.WriteHeader(status)
	if r.Method != "HEAD" {
		w.Write(body)
	}
	return status
}

// ---------------------------------------------------------------------------
// CGI response parsing
// ---------------------------------------------------------------------------

// writeCGIResponse inspects the handler output for optional CGI-style headers.
// If the output starts with valid header lines followed by a blank line, those
// lines are parsed as response headers. Otherwise the entire output is the body.
func writeCGIResponse(w http.ResponseWriter, raw []byte, exitCode int) int {
	// Defaults.
	status := exitToStatus(exitCode)
	contentType := "application/json"
	body := raw
	headersParsed := false

	if len(raw) > 0 && looksLikeHeader(raw) {
		sc := bufio.NewScanner(bytes.NewReader(raw))
		consumed := 0
		for sc.Scan() {
			line := sc.Text()
			consumed += len(line) + 1 // approximate; accounts for \n

			if line == "" {
				// Blank line terminates headers.
				headersParsed = true
				break
			}

			key, val, ok := parseHeaderLine(line)
			if !ok {
				break // not a header → treat everything as body
			}
			switch strings.ToLower(key) {
			case "status":
				if code, err := strconv.Atoi(strings.Fields(val)[0]); err == nil {
					status = code
				}
			case "content-type":
				contentType = val
			default:
				w.Header().Set(key, val)
			}
		}
		if headersParsed {
			body = raw[consumed:]
		}
	}

	w.Header().Set("Content-Type", contentType)
	w.WriteHeader(status)
	w.Write(body)
	return status
}

// looksLikeHeader returns true if the first line of b matches "Token:".
func looksLikeHeader(b []byte) bool {
	for i, c := range b {
		switch {
		case c == ':' && i > 0:
			return true
		case c == '\n', c == '\r':
			return false
		case c == ' ' || c == '\t':
			return false // spaces before colon → not a header
		}
	}
	return false
}

func parseHeaderLine(line string) (string, string, bool) {
	i := strings.IndexByte(line, ':')
	if i <= 0 {
		return "", "", false
	}
	key := line[:i]
	for _, c := range key {
		if c <= ' ' || c == 127 {
			return "", "", false
		}
	}
	return key, strings.TrimSpace(line[i+1:]), true
}

func exitToStatus(code int) int {
	switch code {
	case 0:
		return 200
	case 1:
		return 400
	default:
		return 500
	}
}

// ---------------------------------------------------------------------------
// Environment construction
// ---------------------------------------------------------------------------

func buildEnv(r *http.Request, params map[string]string) []string {
	env := os.Environ()

	// CGI standard variables.
	env = append(env,
		"REQUEST_METHOD="+r.Method,
		"REQUEST_URI="+r.RequestURI,
		"REQUEST_PATH="+r.URL.Path,
		"QUERY_STRING="+r.URL.RawQuery,
		"CONTENT_TYPE="+r.Header.Get("Content-Type"),
		"CONTENT_LENGTH="+r.Header.Get("Content-Length"),
		"REMOTE_ADDR="+r.RemoteAddr,
	)

	if host, port, err := net.SplitHostPort(r.Host); err == nil {
		env = append(env, "SERVER_NAME="+host, "SERVER_PORT="+port)
	} else {
		env = append(env, "SERVER_NAME="+r.Host)
	}

	// Path parameters: PARAM_HOSTNAME, PARAM_RUN_ID, etc.
	// Hyphens become underscores so that shell scripts can reference them.
	for k, v := range params {
		env = append(env, "PARAM_"+envKey(k)+"="+v)
	}

	// Query parameters: QUERY_STATUS, QUERY_RACK, etc. (first value per key).
	for k, vs := range r.URL.Query() {
		env = append(env, "QUERY_"+envKey(k)+"="+vs[0])
	}

	// Forward all request headers as HTTP_ACCEPT, HTTP_AUTHORIZATION, etc.
	for k, vs := range r.Header {
		name := "HTTP_" + strings.ToUpper(strings.ReplaceAll(k, "-", "_"))
		env = append(env, name+"="+vs[0])
	}

	return env
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

func writeJSON(w http.ResponseWriter, status int, format string, args ...any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	fmt.Fprintf(w, format, args...)
}

func sortedKeys(m map[string]string) []string {
	keys := make([]string, 0, len(m))
	for k := range m {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	return keys
}

func findDirectoryIndex(dirPath string) (kind string, path string, ok bool) {
	for _, name := range []string{"index.html", "index.htm"} {
		candidate := filepath.Join(dirPath, name)
		if info, err := os.Stat(candidate); err == nil && info.Mode().IsRegular() {
			return "static", candidate, true
		}
	}
	entries, err := os.ReadDir(dirPath)
	if err != nil {
		return "", "", false
	}
	for _, entry := range entries {
		name := entry.Name()
		if entry.IsDir() || !strings.HasPrefix(name, "index.") {
			continue
		}
		candidate := filepath.Join(dirPath, name)
		if info, err := os.Stat(candidate); err == nil && info.Mode().IsRegular() && info.Mode()&0111 != 0 {
			return "exec", candidate, true
		}
	}
	return "", "", false
}

// envKey converts a parameter or query name to a valid env var suffix:
// uppercase, hyphens replaced with underscores.
func envKey(s string) string {
	return strings.ToUpper(strings.ReplaceAll(s, "-", "_"))
}

func envOr(key, fallback string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return fallback
}

// ---------------------------------------------------------------------------
// Main
// ---------------------------------------------------------------------------

func main() {
	routeDir := envOr("ROUTE_DIR", "./routes")
	addr := envOr("LISTEN_ADDR", ":8080")
	timeoutSec, _ := strconv.Atoi(envOr("COMMAND_TIMEOUT", "30"))
	if timeoutSec <= 0 {
		timeoutSec = 30
	}

	root := newNode()
	if err := buildTree(root, routeDir); err != nil {
		log.Fatalf("failed to scan %s: %v", routeDir, err)
	}

	log.Printf("routes from %s:", routeDir)
	printRoutes(root, "")

	srv := &http.Server{
		Addr: addr,
		Handler: &server{
			root:     root,
			timeout:  time.Duration(timeoutSec) * time.Second,
			routeDir: func() string { abs, _ := filepath.Abs(routeDir); return abs }(),
		},
		ReadTimeout:  10 * time.Second,
		WriteTimeout: time.Duration(timeoutSec+5) * time.Second,
	}

	// Graceful shutdown on SIGINT/SIGTERM.
	done := make(chan os.Signal, 1)
	signal.Notify(done, syscall.SIGINT, syscall.SIGTERM)
	go func() {
		<-done
		log.Println("shutting down...")
		ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
		defer cancel()
		srv.Shutdown(ctx)
	}()

	log.Printf("listening on %s (timeout %ds)", addr, timeoutSec)
	if err := srv.ListenAndServe(); err != http.ErrServerClosed {
		log.Fatal(err)
	}
}
