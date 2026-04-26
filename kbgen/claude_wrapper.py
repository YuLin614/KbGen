from __future__ import annotations

import dataclasses
import http.client
import http.server
import json
import os
import shutil
import socketserver
import subprocess
import sys
import threading
import time

UPSTREAM_HOST = "api.anthropic.com"
UPSTREAM_PORT = 443
PROXY_BIND = "127.0.0.1"

_HOP_BY_HOP = frozenset({
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade", "host",
})


@dataclasses.dataclass
class TokenUsage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_tokens: int = 0
    cache_read_tokens: int = 0
    request_count: int = 0
    _lock: threading.Lock = dataclasses.field(
        default_factory=threading.Lock, repr=False, compare=False
    )

    def add(self, input_t: int, output_t: int, cache_create: int, cache_read: int) -> None:
        with self._lock:
            self.input_tokens += input_t
            self.output_tokens += output_t
            self.cache_creation_tokens += cache_create
            self.cache_read_tokens += cache_read
            self.request_count += 1

    def format_summary(self, elapsed_sec: float) -> str:
        total_input = self.input_tokens + self.cache_creation_tokens + self.cache_read_tokens
        lines = [
            "--- kbclaude session summary ---",
            f"  Requests          : {self.request_count:,}",
            f"  Input tokens      : {self.input_tokens:,}  (uncached)",
            f"  Cache write tokens: {self.cache_creation_tokens:,}",
            f"  Cache read tokens : {self.cache_read_tokens:,}",
            f"  Total input       : {total_input:,}  (uncached + cache_write + cache_read)",
            f"  Output tokens     : {self.output_tokens:,}",
            f"  Duration          : {elapsed_sec:.1f}s",
            "--------------------------------",
        ]
        return "\n".join(lines)


def _extract_usage_from_sse_line(line: bytes) -> dict:
    """Parse token counts from one SSE data line. Returns {} on any error."""
    try:
        text = line.decode("utf-8", errors="replace").strip()
        if not text.startswith("data:"):
            return {}
        payload = text[5:].strip()
        if not payload or payload == "[DONE]":
            return {}
        obj = json.loads(payload)
        event_type = obj.get("type", "")
        if event_type == "message_start":
            u = obj.get("message", {}).get("usage", {})
            return {
                "input_tokens": u.get("input_tokens", 0),
                "cache_creation_tokens": u.get("cache_creation_input_tokens", 0),
                "cache_read_tokens": u.get("cache_read_input_tokens", 0),
            }
        if event_type == "message_delta":
            u = obj.get("usage", {})
            return {"output_tokens": u.get("output_tokens", 0)}
    except Exception:
        pass
    return {}


def _extract_usage_from_json_body(body: bytes) -> dict:
    """Parse token counts from a non-streaming JSON response body."""
    try:
        obj = json.loads(body)
        u = obj.get("usage", {})
        return {
            "input_tokens": u.get("input_tokens", 0),
            "output_tokens": u.get("output_tokens", 0),
            "cache_creation_tokens": u.get("cache_creation_input_tokens", 0),
            "cache_read_tokens": u.get("cache_read_input_tokens", 0),
        }
    except Exception:
        return {}


class ProxyHandler(http.server.BaseHTTPRequestHandler):
    usage: TokenUsage  # injected per-server via _make_server subclass

    def log_message(self, *args) -> None:
        pass  # suppress per-request noise

    def do_GET(self) -> None:
        self._forward_request("GET")

    def do_POST(self) -> None:
        self._forward_request("POST")

    def do_DELETE(self) -> None:
        self._forward_request("DELETE")

    def do_OPTIONS(self) -> None:
        self._forward_request("OPTIONS")

    def do_PATCH(self) -> None:
        self._forward_request("PATCH")

    def _forward_request(self, method: str) -> None:
        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length) if content_length > 0 else b""

        headers = {
            k: v for k, v in self.headers.items()
            if k.lower() not in _HOP_BY_HOP
        }
        headers["Host"] = UPSTREAM_HOST

        try:
            conn = http.client.HTTPSConnection(UPSTREAM_HOST, UPSTREAM_PORT, timeout=120)
            conn.request(method, self.path, body=body, headers=headers)
            resp = conn.getresponse()
        except OSError as exc:
            self.send_error(502, f"Upstream error: {exc}")
            return

        resp_headers = [
            (k, v) for k, v in resp.getheaders()
            if k.lower() not in _HOP_BY_HOP
        ]
        content_type = resp.getheader("Content-Type", "")

        if "text/event-stream" in content_type:
            self._forward_sse_response(resp, resp_headers)
        else:
            self._forward_json_response(resp, resp_headers)

        conn.close()

    def _forward_sse_response(self, resp: http.client.HTTPResponse, resp_headers: list) -> None:
        self.send_response(resp.status, resp.reason)
        for name, value in resp_headers:
            self.send_header(name, value)
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()

        req_input = req_output = req_cache_create = req_cache_read = 0
        try:
            while True:
                line = resp.readline()
                if not line:
                    break
                tokens = _extract_usage_from_sse_line(line)
                req_input += tokens.get("input_tokens", 0)
                req_output += tokens.get("output_tokens", 0)
                req_cache_create += tokens.get("cache_creation_tokens", 0)
                req_cache_read += tokens.get("cache_read_tokens", 0)

                chunk = hex(len(line))[2:].encode("ascii") + b"\r\n" + line + b"\r\n"
                self.wfile.write(chunk)
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass

        try:
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass

        if req_input or req_output or req_cache_create or req_cache_read:
            self.usage.add(req_input, req_output, req_cache_create, req_cache_read)

    def _forward_json_response(self, resp: http.client.HTTPResponse, resp_headers: list) -> None:
        body = resp.read()
        tokens = _extract_usage_from_json_body(body)

        self.send_response(resp.status, resp.reason)
        for name, value in resp_headers:
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass

        if any(tokens.get(k, 0) for k in ("input_tokens", "output_tokens",
                                           "cache_creation_tokens", "cache_read_tokens")):
            self.usage.add(
                tokens.get("input_tokens", 0),
                tokens.get("output_tokens", 0),
                tokens.get("cache_creation_tokens", 0),
                tokens.get("cache_read_tokens", 0),
            )


def _make_server(usage: TokenUsage) -> socketserver.ThreadingTCPServer:
    class _Handler(ProxyHandler):
        pass
    _Handler.usage = usage
    socketserver.ThreadingTCPServer.allow_reuse_address = True
    server = socketserver.ThreadingTCPServer((PROXY_BIND, 0), _Handler)
    return server


def find_claude() -> str:
    path = shutil.which("claude")
    if not path:
        print(
            "error: 'claude' not found in PATH. Install Claude Code CLI first.",
            file=sys.stderr,
        )
        raise SystemExit(1)
    return path


def run_claude_with_proxy(claude_args: list[str]) -> int:
    claude_path = find_claude()
    usage = TokenUsage()
    server = _make_server(usage)
    port = server.server_address[1]

    ready = threading.Event()

    def _serve() -> None:
        ready.set()
        server.serve_forever()

    server_thread = threading.Thread(target=_serve, name="kbclaude-proxy", daemon=True)
    server_thread.start()
    ready.wait(timeout=5.0)

    proc = subprocess.Popen(
        [claude_path, *claude_args],
        env={**os.environ, "ANTHROPIC_BASE_URL": f"http://{PROXY_BIND}:{port}"},
    )
    start = time.monotonic()
    exit_code = 0
    try:
        exit_code = proc.wait()
    except KeyboardInterrupt:
        try:
            exit_code = proc.wait(timeout=5.0)
        except subprocess.TimeoutExpired:
            proc.terminate()
            exit_code = proc.wait()
    finally:
        elapsed = time.monotonic() - start
        server.shutdown()
        server.server_close()
        print(f"\n{usage.format_summary(elapsed)}", file=sys.stderr)

    return exit_code


def main(argv: list[str] | None = None) -> None:
    import argparse
    parser = argparse.ArgumentParser(
        prog="kbclaude",
        description="Run claude CLI with Anthropic token usage tracking",
    )
    parser.add_argument(
        "args",
        nargs=argparse.REMAINDER,
        help="Arguments forwarded verbatim to the claude CLI",
    )
    args = parser.parse_args(argv)
    claude_args = list(args.args)
    if claude_args and claude_args[0] == "--":
        claude_args = claude_args[1:]
    raise SystemExit(run_claude_with_proxy(claude_args))
