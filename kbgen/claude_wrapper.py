from __future__ import annotations

import dataclasses
import datetime
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
from pathlib import Path

UPSTREAM_HOST = "api.anthropic.com"
UPSTREAM_PORT = 443
PROXY_BIND = "127.0.0.1"

# Injected into the system prompt of the first API call when a snapshot exists.
_SNAPSHOT_INJECT = (
    "\n\n## Codebase navigation\n"
    "At the start of a new feature development session, read `.ai/snapshot.kb` "
    "(schema: `.ai/schema.kb`) once before writing any code. Use it for navigation "
    "only — finding file locations, not understanding logic. Key fields: "
    "`a` (symbol→file:line), `p` (file inventory per module), `ri` (route→file mapping), "
    "`hf`/`hr` (task entry points by type), `fd` (file dependency edges). "
    "If a path from snapshot is not found on disk, fall back to `Glob` — snapshot may be stale mid-session."
)

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
    usage: TokenUsage        # injected per-server via _make_server subclass
    snapshot_path: Path      # injected per-server; empty Path means no snapshot
    _injected: bool = False  # class-level default; overridden per-server instance
    _inject_lock: threading.Lock  # injected per-server

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

    def _maybe_inject(self, body: bytes) -> bytes:
        """Inject snapshot instruction into system prompt on the first /v1/messages POST."""
        if not self.snapshot_path.exists():
            return body
        with self._inject_lock:
            if self._inject_state["done"]:  # type: ignore[attr-defined]
                return body
            self._inject_state["done"] = True  # type: ignore[attr-defined]
        try:
            obj = json.loads(body)
        except Exception:
            return body
        if not isinstance(obj, dict):
            return body
        existing = obj.get("system", "")
        if isinstance(existing, str):
            obj["system"] = existing + _SNAPSHOT_INJECT
        elif isinstance(existing, list):
            # structured system blocks — append a plain text block
            obj["system"] = existing + [{"type": "text", "text": _SNAPSHOT_INJECT.strip()}]
        else:
            obj["system"] = _SNAPSHOT_INJECT.strip()
        return json.dumps(obj).encode("utf-8")

    def _forward_request(self, method: str) -> None:
        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length) if content_length > 0 else b""

        # Inject snapshot guidance into the very first /v1/messages call
        if method == "POST" and self.path.startswith("/v1/messages"):
            body = self._maybe_inject(body)

        headers = {
            k: v for k, v in self.headers.items()
            if k.lower() not in _HOP_BY_HOP
        }
        headers["Host"] = UPSTREAM_HOST
        if body:
            headers["Content-Length"] = str(len(body))

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


def _make_server(usage: TokenUsage, snapshot_path: Path) -> socketserver.ThreadingTCPServer:
    lock = threading.Lock()
    inject_state = {"done": False}

    class _Handler(ProxyHandler):
        pass
    _Handler.usage = usage
    _Handler.snapshot_path = snapshot_path
    _Handler._inject_lock = lock
    _Handler._inject_state = inject_state  # type: ignore[attr-defined]
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


def _sessions_log_path() -> Path:
    return Path.home() / ".kbgen" / "sessions.jsonl"


def _persist_session(usage: "TokenUsage", elapsed_sec: float, cwd: Path) -> None:
    """Append one session record to ~/.kbgen/sessions.jsonl (best-effort)."""
    try:
        log_path = _sessions_log_path()
        log_path.parent.mkdir(parents=True, exist_ok=True)
        snapshot_present = (cwd / ".ai" / "snapshot.kb").exists()
        record = {
            "ts": datetime.datetime.now().isoformat(timespec="seconds"),
            "duration_s": round(elapsed_sec, 1),
            "requests": usage.request_count,
            "input_tokens": usage.input_tokens,
            "output_tokens": usage.output_tokens,
            "cache_write_tokens": usage.cache_creation_tokens,
            "cache_read_tokens": usage.cache_read_tokens,
            "snapshot": snapshot_present,
            "cwd": str(cwd),
        }
        with log_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record) + "\n")
    except Exception:
        pass  # never break the user's session over logging


_SNAPSHOT_FRESH_SECONDS = 5 * 60  # skip re-scan if snapshot is younger than this


def _snapshot_is_fresh(snapshot_path: Path) -> bool:
    """Return True if snapshot exists and was modified within the freshness window."""
    try:
        age = time.time() - snapshot_path.stat().st_mtime
        return age < _SNAPSHOT_FRESH_SECONDS
    except OSError:
        return False


def _auto_scan(cwd: Path) -> Path:
    """Run kbgen update (or scan if no snapshot) before starting claude. Returns snapshot path."""
    ai_dir = cwd / ".ai"
    snapshot_path = ai_dir / "snapshot.kb"
    try:
        if snapshot_path.exists():
            if _snapshot_is_fresh(snapshot_path):
                age_s = int(time.time() - snapshot_path.stat().st_mtime)
                print(f"[kbclaude] snapshot is fresh ({age_s}s old), skipping scan", file=sys.stderr)
                return snapshot_path
            cmd = [sys.executable, "-m", "kbgen", "--root", str(cwd), "update"]
            label = "update"
        else:
            ai_dir.mkdir(parents=True, exist_ok=True)
            cmd = [sys.executable, "-m", "kbgen", "--root", str(cwd), "scan"]
            label = "scan"
        print(f"[kbclaude] running kbgen {label}...", file=sys.stderr)
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            print(f"[kbclaude] kbgen {label} failed (non-fatal):\n{result.stderr}", file=sys.stderr)
        else:
            print(f"[kbclaude] snapshot ready", file=sys.stderr)
    except Exception as exc:
        print(f"[kbclaude] auto-scan error (non-fatal): {exc}", file=sys.stderr)
    return snapshot_path


def run_claude_with_proxy(claude_args: list[str], auto_scan: bool = True) -> int:
    claude_path = find_claude()
    cwd = Path.cwd()

    snapshot_path = _auto_scan(cwd) if auto_scan else (cwd / ".ai" / "snapshot.kb")

    usage = TokenUsage()
    server = _make_server(usage, snapshot_path)
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
        _persist_session(usage, elapsed, cwd)
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
