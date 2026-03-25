#!/usr/bin/env perl
use strict;
use warnings;
use Cwd qw(abs_path getcwd);
use Errno qw(EINTR);
use File::Find;
use File::Spec;
use IO::Handle;
use IO::Select;
use IO::Socket::INET;
use POSIX qw(:sys_wait_h WNOHANG _exit setsid strftime dup2);
use Socket qw(SOL_SOCKET SO_REUSEADDR);
use Time::HiRes qw(time sleep);

my %HTTP_METHODS = map { $_ => 1 } qw(GET HEAD POST PUT DELETE PATCH OPTIONS);
my %MIME_TYPES = (
    '.json' => 'application/json',
    '.txt'  => 'text/plain',
    '.html' => 'text/html',
    '.js'   => 'application/javascript',
    '.css'  => 'text/css',
    '.xml'  => 'application/xml',
    '.png'  => 'image/png',
    '.jpg'  => 'image/jpeg',
    '.jpeg' => 'image/jpeg',
    '.gif'  => 'image/gif',
    '.svg'  => 'image/svg+xml',
);

sub env_or {
    my ($key, $fallback) = @_;
    my $value = $ENV{$key};
    return defined($value) && $value ne '' ? $value : $fallback;
}

sub uri_unescape {
    my ($value) = @_;
    $value = '' if !defined $value;
    $value =~ s/%([0-9A-Fa-f]{2})/chr(hex($1))/ge;
    return $value;
}

sub parse_timeout {
    my ($value) = @_;
    return 30 if !defined($value) || $value !~ /^\d+$/ || $value <= 0;
    return int($value);
}

sub normalize_request_path {
    my ($path) = @_;
    $path =~ s{/+}{/}g;
    $path = '/' if $path eq '';
    $path =~ s{/$}{} if $path ne '/';
    my @segs;
    for my $segment (split m{/}, $path) {
        next if $segment eq '';
        my $decoded = uri_unescape($segment);
        die "invalid_path\n" if $decoded eq '..';
        push @segs, $decoded;
    }
    return \@segs;
}

sub new_node {
    my ($param_name) = @_;
    return {
        literal => {},
        param => undef,
        param_name => defined($param_name) ? $param_name : '',
        handlers => {},
    };
}

sub build_tree {
    my ($route_dir) = @_;
    my $abs_dir = abs_path($route_dir);
    die "route_dir_not_found\n" if !defined $abs_dir || !-d $abs_dir;

    my $root = new_node();
    find(
        {
            follow => 1,
            no_chdir => 1,
            wanted => sub {
                return if !-f $_;
                my $full = $File::Find::name;
                my ($filename) = $full =~ m{([^/]+)$};
                my $method = uc($filename // '');
                return if !$HTTP_METHODS{$method};

                my $parent = $full;
                $parent =~ s{[^/]+$}{};
                $parent =~ s{/$}{};
                my $rel = File::Spec->abs2rel($parent, $abs_dir);
                my @parts = grep { $_ ne '' && $_ ne '.' } File::Spec->splitdir($rel);
                my $cur = $root;
                for my $seg (@parts) {
                    if ($seg =~ /^:(.+)$/) {
                        if (!defined $cur->{param}) {
                            $cur->{param} = new_node($1);
                        }
                        $cur = $cur->{param};
                    } else {
                        $cur->{literal}{$seg} ||= new_node();
                        $cur = $cur->{literal}{$seg};
                    }
                }
                $cur->{handlers}{$method} = $full;
            },
        },
        $abs_dir,
    );
    return ($root, $abs_dir);
}

sub match_node {
    my ($root, $segs) = @_;
    my %params;
    my $cur = $root;
    for my $seg (@{$segs}) {
        if (exists $cur->{literal}{$seg}) {
            $cur = $cur->{literal}{$seg};
        } elsif (defined $cur->{param}) {
            $params{$cur->{param}{param_name}} = $seg;
            $cur = $cur->{param};
        } else {
            return;
        }
    }
    return ($cur, \%params);
}

sub join_prefix {
    my ($prefix, $seg) = @_;
    return $prefix eq '' ? $seg : "$prefix/$seg";
}

sub is_executable {
    my ($path) = @_;
    my @st = stat($path);
    return 0 if !@st;
    return ($st[2] & 0111) ? 1 : 0;
}

sub collect_routes {
    my ($node, $prefix, $items) = @_;
    my $route = $prefix eq '' ? '/' : "/$prefix";
    for my $method (sort keys %{ $node->{handlers} }) {
        my $path = $node->{handlers}{$method};
        my $tag = eval { is_executable($path) ? 'exec' : 'static' } || 'unknown';
        push @{$items}, [$route, $method, $path, $tag];
    }
    for my $seg (sort keys %{ $node->{literal} }) {
        collect_routes($node->{literal}{$seg}, join_prefix($prefix, $seg), $items);
    }
    if (defined $node->{param}) {
        collect_routes($node->{param}, join_prefix($prefix, ':' . $node->{param}{param_name}), $items);
    }
}

sub print_routes {
    my ($root, $route_dir) = @_;
    print STDERR "routes from $route_dir:\n";
    my @items;
    collect_routes($root, '', \@items);
    @items = sort {
        $a->[0] cmp $b->[0] || $a->[1] cmp $b->[1]
    } @items;
    for my $item (@items) {
        printf STDERR "  %-7s %-45s → %s [%s]\n", $item->[1], $item->[0], $item->[2], $item->[3];
    }
    STDERR->flush();
}

sub split_host_port {
    my ($value) = @_;
    return ('', '') if !defined($value) || $value eq '';
    if ($value =~ /^\[([^\]]+)\](?::(.+))?$/) {
        return ($1, defined($2) ? $2 : '');
    }
    if ($value =~ /^(.*):([^:]+)$/ && $value !~ /:.*:/) {
        return ($1, $2);
    }
    return ($value, '');
}

sub env_key {
    my ($value) = @_;
    $value =~ tr/-/_/;
    return uc($value);
}

sub parse_query_pairs {
    my ($raw_query) = @_;
    return [] if !defined($raw_query) || $raw_query eq '';
    my @pairs;
    for my $pair (split /&/, $raw_query) {
        my ($k, $v) = split /=/, $pair, 2;
        $k = '' if !defined $k;
        $v = '' if !defined $v;
        $k =~ s/\+/ /g;
        $v =~ s/\+/ /g;
        push @pairs, [uri_unescape($k), uri_unescape($v)];
    }
    return \@pairs;
}

sub build_env {
    my ($request, $params, $listen_addr) = @_;
    my %env = %ENV;
    $env{REQUEST_METHOD} = $request->{method};
    $env{REQUEST_URI} = $request->{target};
    $env{REQUEST_PATH} = $request->{path};
    $env{QUERY_STRING} = defined($request->{query}) ? $request->{query} : '';
    $env{CONTENT_TYPE} = $request->{headers}{'content-type'} // '';
    $env{CONTENT_LENGTH} = $request->{headers}{'content-length'} // '';
    $env{REMOTE_ADDR} = $request->{remote_addr} // '';

    my ($server_name, $server_port) = split_host_port(($request->{headers}{host} // '') ne '' ? $request->{headers}{host} : $listen_addr);
    $env{SERVER_NAME} = $server_name;
    $env{SERVER_PORT} = $server_port if $server_port ne '';

    for my $key (keys %{$params}) {
        $env{'PARAM_' . env_key($key)} = $params->{$key};
    }

    my %seen_query;
    for my $pair (@{ parse_query_pairs($request->{query}) }) {
        my ($key, $value) = @{$pair};
        next if $seen_query{$key}++;
        $env{'QUERY_' . env_key($key)} = $value;
    }

    my %seen_headers;
    for my $key (keys %{ $request->{headers} }) {
        my $lower = lc($key);
        next if $seen_headers{$lower}++;
        $env{'HTTP_' . env_key($key)} = $request->{headers}{$key};
    }

    return \%env;
}

sub exit_to_status {
    my ($code) = @_;
    return 200 if $code == 0;
    return 400 if $code == 1;
    return 500;
}

sub content_type_for {
    my ($path) = @_;
    if ($path =~ /(\.[^.\/]+)$/) {
        return $MIME_TYPES{lc($1)} // 'application/octet-stream';
    }
    return 'application/octet-stream';
}

sub read_file {
    my ($path) = @_;
    open my $fh, '<:raw', $path or die "$!";
    local $/;
    my $data = <$fh>;
    close $fh;
    return defined($data) ? $data : '';
}

sub shell_quote {
    my ($value) = @_;
    $value = '' if !defined $value;
    $value =~ s/'/'"'"'/g;
    return "'$value'";
}

sub run_handler {
    my ($handler_path, $request_body, $env, $timeout_seconds) = @_;
    my $stdin_path = File::Spec->catfile(File::Spec->tmpdir(), "fsrouter-perl-stdin-$$-" . int(rand(1_000_000)));
    my $stdout_path = File::Spec->catfile(File::Spec->tmpdir(), "fsrouter-perl-stdout-$$-" . int(rand(1_000_000)));
    my $stderr_path = File::Spec->catfile(File::Spec->tmpdir(), "fsrouter-perl-stderr-$$-" . int(rand(1_000_000)));

    open my $stdin_fh, '>:raw', $stdin_path or die "$!";
    print {$stdin_fh} $request_body;
    close $stdin_fh;

    my @env_parts;
    for my $key (sort keys %{$env}) {
        push @env_parts, $key . '=' . shell_quote($env->{$key});
    }

    my $cwd = $handler_path;
    $cwd =~ s{[^/]+$}{};
    $cwd =~ s{/$}{};

    my $command = join '',
        'cd ', shell_quote($cwd),
        ' && env -i ', join(' ', @env_parts),
        ' perl -e ', shell_quote('alarm shift; exec @ARGV'),
        ' ', shell_quote($timeout_seconds),
        ' ', shell_quote($handler_path),
        ' <', shell_quote($stdin_path),
        ' >', shell_quote($stdout_path),
        ' 2>', shell_quote($stderr_path);

    my ($ok, $reason, $code);
    {
        local $@;
        ($ok, $reason, $code) = system($command);
        if ($? == -1) {
            $ok = 0;
            $reason = 'exec_failed';
            $code = 127;
        } else {
            $code = $? >> 8;
            $reason = ($? & 127) ? 'signal' : 'exit';
            $ok = ($? == 0) ? 1 : 0;
        }
    }

    my $stdout = eval { read_file($stdout_path) } // '';
    my $stderr = eval { read_file($stderr_path) } // '';
    unlink $stdin_path;
    unlink $stdout_path;
    unlink $stderr_path;

    my $timed_out = ($code == 142 || $code == 124) ? 1 : 0;
    return {
        ok => $ok,
        reason => $reason,
        exit_code => $code,
        stdout => $stdout,
        stderr => $stderr,
        timed_out => $timed_out,
    };
}

sub status_reason {
    my ($status) = @_;
    my %map = (
        200 => 'OK',
        400 => 'Bad Request',
        404 => 'Not Found',
        405 => 'Method Not Allowed',
        500 => 'Internal Server Error',
        502 => 'Bad Gateway',
        504 => 'Gateway Timeout',
    );
    return $map{$status} // 'OK';
}

sub send_response {
    my ($client, $method, $status, $headers, $body) = @_;
    $headers->{'Content-Length'} = length($body);
    $headers->{'Connection'} = 'close';
    print {$client} sprintf("HTTP/1.1 %d %s\r\n", $status, status_reason($status));
    for my $key (keys %{$headers}) {
        print {$client} "$key: $headers->{$key}\r\n";
    }
    print {$client} "\r\n";
    print {$client} $body if $method ne 'HEAD';
    $client->flush();
}

sub json_escape {
    my ($value) = @_;
    $value =~ s/\\/\\\\/g;
    $value =~ s/"/\\"/g;
    $value =~ s/\n/\\n/g;
    $value =~ s/\r/\\r/g;
    $value =~ s/\t/\\t/g;
    $value =~ s/\f/\\f/g;
    $value =~ s/([\x00-\x08\x0B\x0C\x0E-\x1F])/sprintf('\\u%04x', ord($1))/ge;
    return $value;
}


sub json_encode {
    my ($value) = @_;
    if (!defined $value) {
        return 'null';
    }
    if (!ref($value)) {
        return $value =~ /^-?\d+(?:\.\d+)?$/ ? $value : '"' . json_escape($value) . '"';
    }
    if (ref($value) eq 'ARRAY') {
        return '[' . join(',', map { json_encode($_) } @{$value}) . ']';
    }
    if (ref($value) eq 'HASH') {
        return '{' . join(',', map { json_encode($_) . ':' . json_encode($value->{$_}) } sort keys %{$value}) . '}';
    }
    die 'unsupported_json_type';
}

sub write_json {
    my ($client, $method, $status, $payload, $extra_headers) = @_;
    my $body = json_encode($payload);
    my %headers = %{ $extra_headers || {} };
    $headers{'Content-Type'} = 'application/json';
    send_response($client, $method, $status, \%headers, $body);
    return $status;
}

sub serve_static {
    my ($client, $method, $handler_path) = @_;
    my $data = eval { read_file($handler_path) };
    if ($@) {
        return write_json($client, $method, 500, { error => 'static_read_failed', message => "$@" });
    }
    send_response($client, $method, 200, { 'Content-Type' => content_type_for($handler_path) }, $data);
    return 200;
}

sub execute_handler {
    my ($client, $request, $handler_path, $params, $timeout_seconds, $listen_addr) = @_;
    my $env = build_env($request, $params, $listen_addr);
    my $result = run_handler($handler_path, $request->{body}, $env, $timeout_seconds);
    if ($result->{timed_out}) {
        return write_json($client, $request->{method}, 504, { error => 'handler_timeout', timeout_seconds => $timeout_seconds });
    }
    if (!$result->{ok} && $result->{exit_code} == 127) {
        return write_json($client, $request->{method}, 502, { error => 'exec_failed', message => $result->{reason} });
    }
    if ($result->{stderr} ne '') {
        my $stderr = $result->{stderr};
        $stderr =~ s/[\r\n]+$//;
        print STDERR "  [handler stderr] $stderr\n";
        STDERR->flush();
    }
    my $body = $result->{stdout};
    if ($body eq '' && $result->{exit_code} != 0 && $result->{stderr} ne '') {
        $body = $result->{stderr};
    }
    my $status = exit_to_status($result->{exit_code});
    send_response($client, $request->{method}, $status, { 'Content-Type' => 'application/json' }, $body);
    return $status;
}

sub serve_dir_listing {
    my ($client, $method, $dir_path, $request_path) = @_;
    opendir(my $dh, $dir_path) or do {
        return write_json($client, $method, 500, { error => 'dir_listing_failed', message => "$!" });
    };
    my @entries = sort grep { $_ ne '.' && $_ ne '..' } readdir($dh);
    closedir($dh);
    my $title = 'Index of ' . ($request_path // '/');
    my @lines = ('<!DOCTYPE html><html><head><title>' . $title . '</title></head><body><h1>' . $title . '</h1><ul>');
    push @lines, '<li><a href="../">../</a></li>' if defined($request_path) && $request_path ne '/';
    for my $name (@entries) {
        my $full = $dir_path . '/' . $name;
        if (-d $full) {
            push @lines, '<li><a href="' . $name . '/">' . $name . '/</a></li>';
        } else {
            push @lines, '<li><a href="' . $name . '">' . $name . '</a></li>';
        }
    }
    push @lines, '</ul></body></html>';
    my $body = join("\n", @lines);
    send_response($client, $method, 200, { 'Content-Type' => 'text/html; charset=utf-8' }, $body);
    return 200;
}

sub find_directory_index {
    my ($dir_path) = @_;
    for my $name ('index.html', 'index.htm') {
        my $candidate = File::Spec->catfile($dir_path, $name);
        return ('static', $candidate) if -f $candidate;
    }

    opendir(my $dh, $dir_path) or return;
    my @entries = sort grep { $_ ne '.' && $_ ne '..' } readdir($dh);
    closedir($dh);
    for my $name (@entries) {
        next if $name !~ /^index\./;
        my $candidate = File::Spec->catfile($dir_path, $name);
        return ('exec', $candidate) if -f $candidate && is_executable($candidate);
    }
    return;
}

sub serve_filesystem_fallback {
    my ($client, $request, $segs, $abs_route_dir, $timeout_seconds, $listen_addr) = @_;
    my $fallback = File::Spec->catfile($abs_route_dir, @{$segs});
    $fallback = $abs_route_dir if !@{$segs};
    if (-d $fallback) {
        my ($kind, $candidate) = find_directory_index($fallback);
        return execute_handler($client, $request, $candidate, {}, $timeout_seconds, $listen_addr) if defined($kind) && is_executable($candidate);
        return serve_static($client, $request->{method}, $candidate) if defined($kind);
        return serve_dir_listing($client, $request->{method}, $fallback, $request->{path});
    }
    if (-f $fallback) {
        return execute_handler($client, $request, $fallback, {}, $timeout_seconds, $listen_addr) if is_executable($fallback);
        return serve_static($client, $request->{method}, $fallback);
    }
    return write_json($client, $request->{method}, 404, { error => 'not_found', path => $request->{path} });
}

sub parse_request {
    my ($client) = @_;
    my $request_line = <$client>;
    return if !defined $request_line;
    $request_line =~ s/\r?\n$//;
    my ($method, $target, $version) = $request_line =~ /^(\S+)\s+(\S+)\s+(HTTP\/\d+\.\d+)$/;
    return { error => 'bad_request' } if !defined $method;

    my %headers;
    while (defined(my $line = <$client>)) {
        $line =~ s/\r?\n$//;
        last if $line eq '';
        my ($key, $value) = $line =~ /^([^:]+):\s*(.*)$/;
        next if !defined $key;
        $headers{lc($key)} = $value;
    }

    my ($path, $query) = $target =~ /^([^?]*)(?:\?(.*))?$/;
    $path = '/' if !defined $path || $path eq '';
    $query = '' if !defined $query;
    my $length = $headers{'content-length'} // 0;
    $length = 0 if $length !~ /^\d+$/;
    my $body = '';
    if ($length > 0) {
        my $remaining = $length;
        while ($remaining > 0) {
            my $read = read($client, my $chunk, $remaining);
            last if !defined($read) || $read == 0;
            $body .= $chunk;
            $remaining -= $read;
        }
    }

    return {
        method => uc($method),
        target => $target,
        version => $version,
        headers => \%headers,
        path => $path,
        query => $query,
        body => $body,
    };
}

sub log_result {
    my ($method, $path, $status, $start) = @_;
    my $elapsed = time() - $start;
    printf STDERR "%s %s → %d (%.6fs)\n", $method, $path, $status, $elapsed;
    STDERR->flush();
}

sub handle_handler {
    my ($client, $request, $handler_path, $params, $timeout_seconds, $listen_addr) = @_;
    if (!is_executable($handler_path)) {
        return serve_static($client, $request->{method}, $handler_path);
    }
    return execute_handler($client, $request, $handler_path, $params, $timeout_seconds, $listen_addr);
}

sub parse_listen_addr {
    my ($addr) = @_;
    if ($addr =~ /^:(\d+)$/) {
        return ('0.0.0.0', int($1));
    }
    if ($addr =~ /^\[([^\]]+)\]:(\d+)$/) {
        return ($1, int($2));
    }
    if ($addr =~ /^(.*):(\d+)$/ && $addr !~ /:.*:/) {
        return ($1, int($2));
    }
    return ($addr, 8080);
}

sub main {
    my $route_dir = env_or('ROUTE_DIR', './routes');
    my $listen_addr = env_or('LISTEN_ADDR', ':8080');
    my $timeout_seconds = parse_timeout(env_or('COMMAND_TIMEOUT', '30'));

    my ($root, $abs_route_dir);
    eval { ($root, $abs_route_dir) = build_tree($route_dir); 1 } or do {
        my $err = $@ || 'unknown error';
        chomp $err;
        print STDERR "failed to scan $route_dir: $err\n";
        STDERR->flush();
        return 1;
    };

    print_routes($root, $route_dir);
    my ($host, $port) = parse_listen_addr($listen_addr);
    my $server = IO::Socket::INET->new(
        LocalAddr => $host,
        LocalPort => $port,
        Proto => 'tcp',
        Listen => 16,
        ReuseAddr => 1,
    ) or die "unable to bind: $!\n";

    $SIG{TERM} = $SIG{INT} = sub {
        print STDERR "shutting down...\n";
        STDERR->flush();
        close $server;
        exit 0;
    };

    print STDERR "listening on $listen_addr (timeout ${timeout_seconds}s)\n";
    STDERR->flush();

    while (my $client = $server->accept()) {
        $client->autoflush(1);
        my $peer_host = eval { $client->peerhost() } // '';
        my $peer_port = eval { $client->peerport() } // '';
        my $start = time();
        my $request = parse_request($client);
        if (!defined $request) {
            close $client;
            next;
        }
        if ($request->{error}) {
            write_json($client, 'GET', 400, { error => 'bad_request' });
            close $client;
            next;
        }
        $request->{remote_addr} = "$peer_host:$peer_port";

        my $status;
        eval {
            my $segs = normalize_request_path($request->{path});
            my ($node, $params) = match_node($root, $segs);
            if (!defined $node || !keys %{ $node->{handlers} }) {
                $status = serve_filesystem_fallback($client, $request, $segs, $abs_route_dir, $timeout_seconds, $listen_addr);
                1;
            } else {
                my $handler_path = $node->{handlers}{ $request->{method} };
                if (!defined $handler_path && $request->{method} eq 'HEAD') {
                    $handler_path = $node->{handlers}{GET};
                }
                if (!defined $handler_path) {
                    my @allow = sort keys %{ $node->{handlers} };
                    $status = write_json($client, $request->{method}, 405, { error => 'method_not_allowed', allow => \@allow }, { Allow => join(', ', @allow) });
                    1;
                } else {
                    $status = handle_handler($client, $request, $handler_path, $params || {}, $timeout_seconds, $listen_addr);
                    1;
                }
            }
        } or do {
            my $err = $@ || 'unknown error';
            if ($err =~ /invalid_path/) {
                $status = write_json($client, $request->{method}, 400, { error => 'invalid_path', path => $request->{path} });
            } else {
                chomp $err;
                $status = write_json($client, $request->{method}, 500, { error => 'server_error', message => $err });
            }
        };
        log_result($request->{method}, $request->{path}, $status, $start);
        close $client;
    }

    close $server;
    return 0;
}

exit(main());
