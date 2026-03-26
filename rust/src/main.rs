use axum::{
    body::{to_bytes, Body, Bytes},
    extract::{ConnectInfo, State},
    http::{
        header::{CONTENT_TYPE, HOST},
        request::Parts,
        HeaderValue, Method, Response, StatusCode,
    },
    routing::any,
    Router,
};
use serde_json::json;
use std::{
    collections::{BTreeMap, HashSet},
    error::Error,
    fs,
    net::SocketAddr,
    path::{Path, PathBuf},
    process::Stdio,
    sync::Arc,
    time::{Duration, Instant},
};
use tokio::{
    io::AsyncWriteExt,
    net::TcpListener,
    process::Command,
    time::timeout,
};
use url::form_urlencoded;
use urlencoding::decode;
use walkdir::WalkDir;

const HTTP_METHODS: [&str; 7] = ["GET", "HEAD", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"];

#[derive(Default)]
struct Node {
    literal: BTreeMap<String, Node>,
    param: Option<Box<Node>>,
    param_name: String,
    handlers: BTreeMap<String, PathBuf>,
    implicit_handler: Option<PathBuf>,
}

impl Node {
    fn match_segments<'a>(&'a self, segs: &[String]) -> Option<(&'a Node, BTreeMap<String, String>)> {
        let mut params = BTreeMap::new();
        let mut cur = self;
        for seg in segs {
            if let Some(child) = cur.literal.get(seg) {
                cur = child;
            } else if let Some(param) = cur.param.as_deref() {
                params.insert(param.param_name.clone(), seg.clone());
                cur = param;
            } else {
                return None;
            }
        }
        Some((cur, params))
    }
}

struct AppState {
    root: Arc<Node>,
    timeout: Duration,
    listen_addr: String,
    route_dir: PathBuf,
}

#[tokio::main]
async fn main() -> Result<(), Box<dyn Error>> {
    let route_dir = env_or("ROUTE_DIR", "./routes");
    let addr = env_or("LISTEN_ADDR", ":8080");
    let timeout_sec = env_or("COMMAND_TIMEOUT", "30")
        .parse::<u64>()
        .ok()
        .filter(|value| *value > 0)
        .unwrap_or(30);

    let root = match build_tree(Path::new(&route_dir)) {
        Ok(root) => root,
        Err(err) => {
            eprintln!("failed to scan {route_dir}: {err}");
            std::process::exit(1);
        }
    };

    log_routes(&root, &route_dir);

    let route_dir_abs = std::fs::canonicalize(&route_dir)
        .unwrap_or_else(|_| PathBuf::from(&route_dir));
    let state = Arc::new(AppState {
        root: Arc::new(root),
        timeout: Duration::from_secs(timeout_sec),
        listen_addr: addr.clone(),
        route_dir: route_dir_abs,
    });

    let app = Router::new()
        .fallback(any(handle_request))
        .with_state(state);

    let bind_addr = normalize_listen_addr(&addr);
    let listener = TcpListener::bind(&bind_addr).await?;

    eprintln!("listening on {} (timeout {}s)", addr, timeout_sec);

    axum::serve(listener, app.into_make_service_with_connect_info::<SocketAddr>())
        .with_graceful_shutdown(shutdown_signal())
        .await?;

    Ok(())
}

async fn handle_request(
    State(state): State<Arc<AppState>>,
    ConnectInfo(remote_addr): ConnectInfo<SocketAddr>,
    request: axum::http::Request<Body>,
) -> Response<Body> {
    let start = Instant::now();
    let method = request.method().clone();
    let raw_path = request.uri().path().to_string();

    let segs = match normalize_request_path(&raw_path) {
        Ok(segs) => segs,
        Err(response) => {
            log_request(method.as_str(), &raw_path, response.status(), start.elapsed());
            return response;
        }
    };

    let (parts, body) = request.into_parts();
    let body = match to_bytes(body, usize::MAX).await {
        Ok(body) => body,
        Err(err) => {
            let response = json_response(
                StatusCode::BAD_REQUEST,
                json!({"error":"invalid_body","message":err.to_string()}),
            );
            log_request(method.as_str(), &raw_path, response.status(), start.elapsed());
            return response;
        }
    };

    let (node, params) = match state.root.match_segments(&segs) {
        Some(result) => result,
        None => {
            let response = serve_filesystem(&state, &parts, &body, remote_addr, &segs, &raw_path).await;
            log_request(method.as_str(), &raw_path, response.status(), start.elapsed());
            return response;
        }
    };

    if node.handlers.is_empty() && node.implicit_handler.is_none() {
        let response = serve_filesystem(&state, &parts, &body, remote_addr, &segs, &raw_path).await;
        log_request(method.as_str(), &raw_path, response.status(), start.elapsed());
        return response;
    }

    // Try method handler
    let handler_path = node.handlers.get(method.as_str()).cloned();

    // If no method handler, try implicit
    let handler_path = if let Some(path) = handler_path {
        path
    } else if let Some(ref implicit) = node.implicit_handler {
        implicit.clone()
    } else {
        // Method handlers exist but none match this method
        let allowed: Vec<String> = node.handlers.keys().cloned().collect();
        let mut response = json_response(
            StatusCode::METHOD_NOT_ALLOWED,
            json!({"error":"method_not_allowed","allow":allowed}),
        );
        if let Ok(value) = HeaderValue::from_str(&node.handlers.keys().cloned().collect::<Vec<_>>().join(", ")) {
            response.headers_mut().insert("Allow", value);
        }
        log_request(method.as_str(), &raw_path, response.status(), start.elapsed());
        return response;
    };

    let response = handle_handler(
        &state,
        &parts,
        &body,
        remote_addr,
        &handler_path,
        &params,
    )
    .await;

    log_request(method.as_str(), &raw_path, response.status(), start.elapsed());
    response
}

async fn handle_handler(
    state: &AppState,
    parts: &Parts,
    body: &Bytes,
    remote_addr: SocketAddr,
    path: &Path,
    params: &BTreeMap<String, String>,
) -> Response<Body> {
    let metadata = match fs::metadata(path) {
        Ok(metadata) => metadata,
        Err(err) => {
            return json_response(
                StatusCode::INTERNAL_SERVER_ERROR,
                json!({"error":"handler_stat_failed","message":err.to_string()}),
            );
        }
    };

    if !is_executable(&metadata) {
        return serve_static(path, parts.method == Method::HEAD).await;
    }

    let mut command = Command::new(path);
    command
        .current_dir(path.parent().unwrap_or_else(|| Path::new(".")))
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .kill_on_drop(true);

    for (key, value) in build_env(parts, params, remote_addr, &state.listen_addr) {
        command.env(key, value);
    }

    let mut child = match command.spawn() {
        Ok(child) => child,
        Err(err) => {
            return json_response(
                StatusCode::BAD_GATEWAY,
                json!({"error":"exec_failed","message":err.to_string()}),
            );
        }
    };

    if let Some(mut stdin) = child.stdin.take() {
        let _ = stdin.write_all(body).await;
    }

    let output = match timeout(state.timeout, child.wait_with_output()).await {
        Ok(result) => match result {
            Ok(output) => output,
            Err(err) => {
                return json_response(
                    StatusCode::BAD_GATEWAY,
                    json!({"error":"exec_failed","message":err.to_string()}),
                );
            }
        },
        Err(_) => {
            return json_response(
                StatusCode::GATEWAY_TIMEOUT,
                json!({"error":"handler_timeout","timeout_seconds":state.timeout.as_secs()}),
            );
        }
    };

    if !output.stderr.is_empty() {
        eprintln!(
            "  [handler stderr] {}",
            String::from_utf8_lossy(&output.stderr).trim_end_matches('\n')
        );
    }

    let exit_code = output.status.code().unwrap_or(1);
    let mut raw = output.stdout;
    if raw.is_empty() && exit_code != 0 && !output.stderr.is_empty() {
        raw = output.stderr;
    }

    handler_response(raw, exit_code, parts.method == Method::HEAD)
}

async fn handle_plain_file(
    state: &AppState,
    parts: &Parts,
    body: &Bytes,
    remote_addr: SocketAddr,
    path: &Path,
) -> Response<Body> {
    let metadata = match fs::metadata(path) {
        Ok(metadata) => metadata,
        Err(err) => {
            return json_response(
                StatusCode::INTERNAL_SERVER_ERROR,
                json!({"error":"handler_stat_failed","message":err.to_string()}),
            );
        }
    };

    if !is_executable(&metadata) {
        return serve_static(path, parts.method == Method::HEAD).await;
    }

    let mut command = Command::new(path);
    command
        .current_dir(path.parent().unwrap_or_else(|| Path::new(".")))
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .kill_on_drop(true);

    for (key, value) in build_env(parts, &BTreeMap::new(), remote_addr, &state.listen_addr) {
        command.env(key, value);
    }

    let mut child = match command.spawn() {
        Ok(child) => child,
        Err(err) => {
            return json_response(
                StatusCode::BAD_GATEWAY,
                json!({"error":"exec_failed","message":err.to_string()}),
            );
        }
    };

    if let Some(mut stdin) = child.stdin.take() {
        let _ = stdin.write_all(body).await;
    }

    let output = match timeout(state.timeout, child.wait_with_output()).await {
        Ok(result) => match result {
            Ok(output) => output,
            Err(err) => {
                return json_response(
                    StatusCode::BAD_GATEWAY,
                    json!({"error":"exec_failed","message":err.to_string()}),
                );
            }
        },
        Err(_) => {
            return json_response(
                StatusCode::GATEWAY_TIMEOUT,
                json!({"error":"handler_timeout","timeout_seconds":state.timeout.as_secs()}),
            );
        }
    };

    if !output.stderr.is_empty() {
        eprintln!(
            "  [handler stderr] {}",
            String::from_utf8_lossy(&output.stderr).trim_end_matches('\n')
        );
    }

    let exit_code = output.status.code().unwrap_or(1);
    let mut raw = output.stdout;
    if raw.is_empty() && exit_code != 0 && !output.stderr.is_empty() {
        raw = output.stderr;
    }

    handler_response(raw, exit_code, parts.method == Method::HEAD)
}

async fn serve_static(path: &Path, suppress_body: bool) -> Response<Body> {
    let data = match tokio::fs::read(path).await {
        Ok(data) => data,
        Err(err) => {
            return json_response(
                StatusCode::INTERNAL_SERVER_ERROR,
                json!({"error":"static_read_failed","message":err.to_string()}),
            );
        }
    };

    let content_type = mime_guess::from_path(path)
        .first_or_octet_stream()
        .essence_str()
        .to_string();

    let mut response = Response::new(if suppress_body {
        Body::empty()
    } else {
        Body::from(data)
    });
    *response.status_mut() = StatusCode::OK;
    if let Ok(value) = HeaderValue::from_str(&content_type) {
        response.headers_mut().insert(CONTENT_TYPE, value);
    }
    response
}

fn handler_response(body: Vec<u8>, exit_code: i32, suppress_body: bool) -> Response<Body> {
    let mut response = Response::new(if suppress_body {
        Body::empty()
    } else {
        Body::from(body)
    });
    *response.status_mut() = exit_to_status(exit_code);
    response
        .headers_mut()
        .insert(CONTENT_TYPE, HeaderValue::from_static("application/json"));
    response
}

fn exit_to_status(code: i32) -> StatusCode {
    match code {
        0 => StatusCode::OK,
        1 => StatusCode::BAD_REQUEST,
        _ => StatusCode::INTERNAL_SERVER_ERROR,
    }
}

fn build_tree(route_dir: &Path) -> std::io::Result<Node> {
    let abs = fs::canonicalize(route_dir)?;
    let mut root = Node::default();

    for entry in WalkDir::new(&abs).follow_links(true) {
        let entry = match entry {
            Ok(entry) => entry,
            Err(err) => {
                eprintln!("warning: {err}");
                continue;
            }
        };

        if entry.file_type().is_dir() {
            continue;
        }

        let method = entry.file_name().to_string_lossy().to_uppercase();
        if HTTP_METHODS.contains(&method.as_str()) {
            let parent = entry.path().parent().unwrap_or(abs.as_path());
            let rel = parent.strip_prefix(&abs).unwrap_or_else(|_| Path::new(""));
            let segs = path_segments(rel);

            let mut cur = &mut root;
            for seg in segs {
                if let Some(name) = seg.strip_prefix(':') {
                    let param = cur.param.get_or_insert_with(|| Box::new(Node::default()));
                    if param.param_name.is_empty() {
                        param.param_name = name.to_string();
                    }
                    cur = param.as_mut();
                } else {
                    cur = cur.literal.entry(seg).or_default();
                }
            }

            cur.handlers.insert(method, entry.path().to_path_buf());
        } else {
            // Check if executable
            if let Ok(metadata) = fs::metadata(entry.path()) {
                if is_executable(&metadata) {
                    let rel = entry.path().strip_prefix(&abs).unwrap_or(Path::new(""));
                    let segs = path_segments(rel);
                    let mut cur = &mut root;
                    for seg in segs {
                        if let Some(name) = seg.strip_prefix(':') {
                            let param = cur.param.get_or_insert_with(|| Box::new(Node::default()));
                            if param.param_name.is_empty() {
                                param.param_name = name.to_string();
                            }
                            cur = param.as_mut();
                        } else {
                            cur = cur.literal.entry(seg).or_default();
                        }
                    }
                    cur.implicit_handler = Some(entry.path().to_path_buf());
                }
            }
        }
    }

    Ok(root)
}

fn path_segments(path: &Path) -> Vec<String> {
    path.components()
        .filter_map(|component| component.as_os_str().to_str().map(ToOwned::to_owned))
        .filter(|segment| !segment.is_empty() && segment != ".")
        .collect()
}

fn normalize_request_path(raw_path: &str) -> Result<Vec<String>, Response<Body>> {
    let mut segs = Vec::new();

    for segment in raw_path.split('/') {
        if segment.is_empty() {
            continue;
        }

        let decoded = match decode(segment) {
            Ok(decoded) => decoded.into_owned(),
            Err(_) => {
                return Err(json_response(
                    StatusCode::BAD_REQUEST,
                    json!({"error":"invalid_path","path":raw_path}),
                ));
            }
        };

        if decoded == ".." {
            return Err(json_response(
                StatusCode::BAD_REQUEST,
                json!({"error":"invalid_path","path":raw_path}),
            ));
        }

        segs.push(decoded);
    }

    Ok(segs)
}

fn build_env(
    parts: &Parts,
    params: &BTreeMap<String, String>,
    remote_addr: SocketAddr,
    listen_addr: &str,
) -> Vec<(String, String)> {
    let mut env = Vec::new();

    env.push(("REQUEST_METHOD".to_string(), parts.method.as_str().to_string()));
    env.push((
        "REQUEST_URI".to_string(),
        parts
            .uri
            .path_and_query()
            .map(|value| value.as_str().to_string())
            .unwrap_or_else(|| parts.uri.path().to_string()),
    ));
    env.push(("REQUEST_PATH".to_string(), parts.uri.path().to_string()));
    env.push((
        "QUERY_STRING".to_string(),
        parts.uri.query().unwrap_or("").to_string(),
    ));
    env.push((
        "CONTENT_TYPE".to_string(),
        header_value(parts, CONTENT_TYPE.as_str()).unwrap_or_default(),
    ));
    env.push((
        "CONTENT_LENGTH".to_string(),
        header_value(parts, "content-length").unwrap_or_default(),
    ));
    env.push(("REMOTE_ADDR".to_string(), remote_addr.to_string()));

    let host = parts
        .headers
        .get(HOST)
        .and_then(|value| value.to_str().ok())
        .unwrap_or(listen_addr);
    let (server_name, server_port) = split_host_port(host);
    env.push(("SERVER_NAME".to_string(), server_name));
    if let Some(port) = server_port {
        env.push(("SERVER_PORT".to_string(), port));
    }

    for (key, value) in params {
        env.push((format!("PARAM_{}", env_key(key)), value.clone()));
    }

    let mut seen_query = HashSet::new();
    if let Some(query) = parts.uri.query() {
        for (key, value) in form_urlencoded::parse(query.as_bytes()) {
            let key = key.into_owned();
            if seen_query.insert(key.clone()) {
                env.push((format!("QUERY_{}", env_key(&key)), value.into_owned()));
            }
        }
    }

    let mut seen_headers = HashSet::new();
    for (name, value) in &parts.headers {
        let key = name.as_str().to_string();
        if !seen_headers.insert(key.clone()) {
            continue;
        }
        if let Ok(value) = value.to_str() {
            env.push((
                format!("HTTP_{}", key.to_uppercase().replace('-', "_")),
                value.to_string(),
            ));
        }
    }

    env
}

fn env_key(value: &str) -> String {
    value.to_uppercase().replace('-', "_")
}

fn header_value(parts: &Parts, name: &str) -> Option<String> {
    parts
        .headers
        .get(name)
        .and_then(|value| value.to_str().ok())
        .map(ToOwned::to_owned)
}

fn split_host_port(host: &str) -> (String, Option<String>) {
    if host.is_empty() {
        return (String::new(), None);
    }

    if host.starts_with('[') {
        if let Some(end) = host.find(']') {
            let name = host[1..end].to_string();
            let port = host[end + 1..].strip_prefix(':').map(ToOwned::to_owned);
            return (name, port);
        }
    }

    if host.matches(':').count() == 1 {
        if let Some((name, port)) = host.rsplit_once(':') {
            return (name.to_string(), Some(port.to_string()));
        }
    }

    (host.to_string(), None)
}

fn normalize_listen_addr(addr: &str) -> String {
    if addr.starts_with(':') {
        format!("0.0.0.0{addr}")
    } else {
        addr.to_string()
    }
}

fn env_or(key: &str, fallback: &str) -> String {
    std::env::var(key).unwrap_or_else(|_| fallback.to_string())
}

fn log_routes(root: &Node, route_dir: &str) {
    eprintln!("routes from {route_dir}:");
    let mut routes = Vec::new();
    collect_routes(root, String::new(), &mut routes);
    routes.sort_by(|a, b| a.1.cmp(&b.1).then_with(|| a.0.cmp(&b.0)));
    for (method, route, path, tag) in routes {
        eprintln!("  {:<7} {:<45} → {} [{}]", method, route, path.display(), tag);
    }
}

fn collect_routes(node: &Node, prefix: String, routes: &mut Vec<(String, String, PathBuf, String)>) {
    let route = if prefix.is_empty() {
        "/".to_string()
    } else {
        format!("/{prefix}")
    };

    for (method, path) in &node.handlers {
        let tag = match fs::metadata(path) {
            Ok(metadata) if is_executable(&metadata) => "exec",
            Ok(_) => "static",
            Err(_) => "unknown",
        }
        .to_string();
        routes.push((method.clone(), route.clone(), path.clone(), tag));
    }

    if let Some(ref implicit) = node.implicit_handler {
        let tag = match fs::metadata(implicit) {
            Ok(metadata) if is_executable(&metadata) => "exec",
            Ok(_) => "static",
            Err(_) => "unknown",
        }.to_string();
        routes.push(("*".to_string(), route.clone(), implicit.clone(), tag));
    }

    for (segment, child) in &node.literal {
        collect_routes(child, join_prefix(&prefix, segment), routes);
    }

    if let Some(param) = node.param.as_deref() {
        collect_routes(param, join_prefix(&prefix, &format!(":{}", param.param_name)), routes);
    }
}

fn join_prefix(prefix: &str, segment: &str) -> String {
    if prefix.is_empty() {
        segment.to_string()
    } else {
        format!("{prefix}/{segment}")
    }
}

async fn serve_filesystem(
    state: &AppState,
    parts: &Parts,
    body: &Bytes,
    remote_addr: SocketAddr,
    segs: &[String],
    raw_path: &str,
) -> Response<Body> {
    let route_dir = &state.route_dir;
    let full_path: PathBuf = segs.iter().fold(route_dir.to_path_buf(), |acc, s| acc.join(s));
    match tokio::fs::metadata(&full_path).await {
        Err(_) => json_response(StatusCode::NOT_FOUND, json!({"error":"not_found","path":raw_path})),
        Ok(meta) if meta.is_file() => handle_plain_file(state, parts, body, remote_addr, &full_path).await,
        Ok(_) => {
            if let Some((kind, path)) = find_directory_index(&full_path).await {
                let _ = kind;
                handle_plain_file(state, parts, body, remote_addr, &path).await
            } else {
                serve_dir_listing(&full_path, raw_path, parts.method == Method::HEAD).await
            }
        }
    }
}

async fn find_directory_index(dir_path: &Path) -> Option<(&'static str, PathBuf)> {
    for name in ["index.html", "index.htm"] {
        let candidate = dir_path.join(name);
        if let Ok(metadata) = tokio::fs::metadata(&candidate).await {
            if metadata.is_file() {
                return Some(("static", candidate));
            }
        }
    }

    let mut rd = tokio::fs::read_dir(dir_path).await.ok()?;
    let mut candidates: Vec<PathBuf> = Vec::new();
    while let Ok(Some(entry)) = rd.next_entry().await {
        let name = entry.file_name();
        let name = name.to_string_lossy();
        if !name.starts_with("index.") {
            continue;
        }
        let path = entry.path();
        if let Ok(metadata) = entry.metadata().await {
            if metadata.is_file() && is_executable(&metadata) {
                candidates.push(path);
            }
        }
    }
    candidates.sort();
    candidates.into_iter().next().map(|path| ("exec", path))
}

async fn serve_dir_listing(dir_path: &Path, request_path: &str, suppress_body: bool) -> Response<Body> {
    let mut rd = match tokio::fs::read_dir(dir_path).await {
        Err(err) => {
            return json_response(
                StatusCode::INTERNAL_SERVER_ERROR,
                json!({"error":"dir_listing_failed","message":err.to_string()}),
            );
        }
        Ok(rd) => rd,
    };
    let mut entries: Vec<(String, bool)> = Vec::new();
    while let Ok(Some(entry)) = rd.next_entry().await {
        let is_dir = entry.file_type().await.map(|t| t.is_dir()).unwrap_or(false);
        entries.push((entry.file_name().to_string_lossy().to_string(), is_dir));
    }
    entries.sort_by(|a, b| a.0.cmp(&b.0));

    let title = format!("Index of {}", request_path);
    let mut html = format!(
        "<!DOCTYPE html><html><head><title>{title}</title></head><body><h1>{title}</h1><ul>"
    );
    if request_path != "/" {
        html.push_str(r#"<li><a href="../">../</a></li>"#);
    }
    for (name, is_dir) in &entries {
        if *is_dir {
            html.push_str(&format!(r#"<li><a href="{name}/">{name}/</a></li>"#));
        } else {
            html.push_str(&format!(r#"<li><a href="{name}">{name}</a></li>"#));
        }
    }
    html.push_str("</ul></body></html>");

    let body_bytes = html.into_bytes();
    let len = body_bytes.len();
    let mut response = Response::new(if suppress_body { Body::empty() } else { Body::from(body_bytes) });
    *response.status_mut() = StatusCode::OK;
    response.headers_mut().insert(CONTENT_TYPE, HeaderValue::from_static("text/html; charset=utf-8"));
    if let Ok(v) = HeaderValue::from_str(&len.to_string()) {
        response.headers_mut().insert("content-length", v);
    }
    response
}

fn json_response(status: StatusCode, value: serde_json::Value) -> Response<Body> {
    let body = serde_json::to_vec(&value).unwrap_or_else(|_| b"{}".to_vec());
    let mut response = Response::new(Body::from(body));
    *response.status_mut() = status;
    response
        .headers_mut()
        .insert(CONTENT_TYPE, HeaderValue::from_static("application/json"));
    response
}

fn log_request(method: &str, path: &str, status: StatusCode, elapsed: Duration) {
    eprintln!("{} {} → {} ({elapsed:?})", method, path, status.as_u16());
}

#[cfg(unix)]
fn is_executable(metadata: &fs::Metadata) -> bool {
    use std::os::unix::fs::PermissionsExt;
    metadata.permissions().mode() & 0o111 != 0
}

#[cfg(not(unix))]
fn is_executable(_: &fs::Metadata) -> bool {
    false
}

async fn shutdown_signal() {
    #[cfg(unix)]
    {
        let mut terminate = tokio::signal::unix::signal(tokio::signal::unix::SignalKind::terminate())
            .expect("failed to install SIGTERM handler");
        tokio::select! {
            _ = tokio::signal::ctrl_c() => {},
            _ = terminate.recv() => {},
        }
    }

    #[cfg(not(unix))]
    {
        let _ = tokio::signal::ctrl_c().await;
    }
}
