from __future__ import annotations

import dataclasses
import datetime
import gzip
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
import zlib
from pathlib import Path

from kbgen.quality import compute_quality

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

_DEFAULT_CONTEXT_LIMIT = 200_000
_MODEL_CONTEXT_LIMIT_PREFIXES: tuple[tuple[str, int], ...] = (
    ("claude-3", 200_000),
    ("claude-sonnet", 200_000),
    ("claude-opus", 200_000),
    ("claude-haiku", 200_000),
)

# Estimated USD pricing per 1M tokens.
_MODEL_PRICING_PER_M: tuple[tuple[str, dict[str, float]], ...] = (
    ("claude-3-7-sonnet", {"input": 3.0, "output": 15.0, "cache_write": 3.75, "cache_read": 0.30}),
    ("claude-3-5-sonnet", {"input": 3.0, "output": 15.0, "cache_write": 3.75, "cache_read": 0.30}),
    ("claude-3-5-haiku", {"input": 0.8, "output": 4.0, "cache_write": 1.0, "cache_read": 0.08}),
    ("claude-3-opus", {"input": 15.0, "output": 75.0, "cache_write": 18.75, "cache_read": 1.50}),
)

_DEFAULT_PRICING_PER_M = {"input": 3.0, "output": 15.0, "cache_write": 3.75, "cache_read": 0.30}

_DEFAULT_REALTIME_USAGE_ENABLED = True
_DEFAULT_REALTIME_USAGE_INTERVAL_SEC = 1.0


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

    def add(self, input_t: int, output_t: int, cache_create: int, cache_read: int) -> int:
        with self._lock:
            self.input_tokens += input_t
            self.output_tokens += output_t
            self.cache_creation_tokens += cache_create
            self.cache_read_tokens += cache_read
            self.request_count += 1
            return self.request_count

    def format_summary(
        self,
        elapsed_sec: float,
        quality: dict | None = None,
        avg_no_snap_input: float | None = None,
    ) -> str:
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
        ]
        if avg_no_snap_input is not None and avg_no_snap_input > 0 and total_input > 0:
            saving_pct = (1 - total_input / avg_no_snap_input) * 100
            direction = "▲" if saving_pct >= 0 else "▼"
            lines.append(f"  Est. saving       : {direction} {abs(saving_pct):.0f}% vs no-snapshot baseline")
        elif avg_no_snap_input is None:
            lines.append("  Est. saving       : n/a (no baseline sessions)")
        if quality and quality.get("available"):
            score = quality["quality_score"]
            grade = quality["grade"]
            stale = quality["stale_files"]
            stale_note = f"  [{stale} files stale — run `kbgen update`]" if stale > 0 else ""
            lines.append(f"  Snapshot quality  : {grade} ({score:.0f}/100){stale_note}")
        lines.append("--------------------------------")
        return "\n".join(lines)


@dataclasses.dataclass
class BudgetTracker:
    budget_usd: float = 0.0
    persist: bool = True
    total_spent_usd: float = 0.0
    session_spent_usd: float = 0.0
    _lock: threading.Lock = dataclasses.field(
        default_factory=threading.Lock, repr=False, compare=False
    )

    @property
    def enabled(self) -> bool:
        return self.budget_usd > 0

    def add_cost(self, amount_usd: float) -> tuple[float, float, float]:
        with self._lock:
            self.session_spent_usd += amount_usd
            self.total_spent_usd += amount_usd
            return self.session_spent_usd, self.total_spent_usd, max(0.0, self.budget_usd - self.total_spent_usd)

    def format_summary(self) -> str:
        if not self.enabled:
            return ""
        remaining = max(0.0, self.budget_usd - self.total_spent_usd)
        lines = [
            "--- kbclaude budget summary ---",
            f"  Budget (USD)      : ${self.budget_usd:,.4f}",
            f"  Session spent     : ${self.session_spent_usd:,.4f}",
            f"  Total spent       : ${self.total_spent_usd:,.4f}",
            f"  Remaining         : ${remaining:,.4f}",
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


def _maybe_decode_for_parsing(body: bytes, content_encoding: str) -> bytes:
    """Decode compressed response body for usage parsing only (best-effort)."""
    enc = (content_encoding or "").strip().lower()
    if not body or not enc:
        return body
    try:
        if enc == "gzip":
            return gzip.decompress(body)
        if enc == "deflate":
            return zlib.decompress(body)
    except Exception:
        return body
    return body


def _make_sse_decoder(content_encoding: str):
    """Return an incremental decoder for compressed SSE payloads, or None."""
    enc = (content_encoding or "").strip().lower()
    if enc == "gzip":
        return zlib.decompressobj(16 + zlib.MAX_WBITS)
    if enc == "deflate":
        return zlib.decompressobj()
    return None


def _extract_model_from_request(body: bytes) -> str | None:
    try:
        payload = json.loads(body)
    except Exception:
        return None
    if not isinstance(payload, dict):
        return None
    model = payload.get("model")
    if isinstance(model, str) and model.strip():
        return model.strip()
    return None


def _context_limit_for_model(model: str | None) -> int:
    if not model:
        return _DEFAULT_CONTEXT_LIMIT
    lower = model.lower()
    for prefix, limit in _MODEL_CONTEXT_LIMIT_PREFIXES:
        if lower.startswith(prefix):
            return limit
    return _DEFAULT_CONTEXT_LIMIT


def _pricing_for_model(model: str | None) -> dict[str, float]:
    if not model:
        return _DEFAULT_PRICING_PER_M
    lower = model.lower()
    for prefix, pricing in _MODEL_PRICING_PER_M:
        if lower.startswith(prefix):
            return pricing
    return _DEFAULT_PRICING_PER_M


def _estimate_request_cost_usd(
    model_name: str | None,
    input_tokens: int,
    output_tokens: int,
    cache_write_tokens: int,
    cache_read_tokens: int,
) -> float:
    pricing = _pricing_for_model(model_name)
    total = 0.0
    total += (input_tokens / 1_000_000) * pricing["input"]
    total += (output_tokens / 1_000_000) * pricing["output"]
    total += (cache_write_tokens / 1_000_000) * pricing["cache_write"]
    total += (cache_read_tokens / 1_000_000) * pricing["cache_read"]
    return total


def _load_realtime_usage_settings() -> tuple[bool, float]:
    enabled_raw = os.environ.get("KBGEN_REALTIME_USAGE", "1").strip().lower()
    enabled = enabled_raw not in {"0", "false", "no", "off"}

    interval = _DEFAULT_REALTIME_USAGE_INTERVAL_SEC
    interval_raw = os.environ.get("KBGEN_REALTIME_USAGE_INTERVAL", "").strip()
    if interval_raw:
        try:
            interval = max(0.2, float(interval_raw))
        except ValueError:
            print(
                f"[kbclaude] invalid KBGEN_REALTIME_USAGE_INTERVAL={interval_raw!r}, using {interval:.1f}s",
                file=sys.stderr,
            )
    return enabled, interval


class ProxyHandler(http.server.BaseHTTPRequestHandler):
    usage: TokenUsage        # injected per-server via _make_server subclass
    snapshot_path: Path      # injected per-server; empty Path means no snapshot
    _injected: bool = False  # class-level default; overridden per-server instance
    _inject_lock: threading.Lock  # injected per-server
    budget: BudgetTracker
    realtime_usage_enabled: bool
    realtime_usage_interval_sec: float

    def log_message(self, *args) -> None:
        pass  # suppress per-request noise

    def _print_status_line(self, message: str) -> None:
        # Force a dedicated line in mixed TUI/terminal output.
        try:
            sys.stderr.write(f"\n{message}\n")
            sys.stderr.flush()
        except Exception:
            pass

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
        model_name: str | None = None

        # Inject snapshot guidance into the very first /v1/messages call
        track_usage = method == "POST" and self.path.startswith("/v1/messages")
        if track_usage:
            body = self._maybe_inject(body)
            model_name = _extract_model_from_request(body)

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
        content_encoding = resp.getheader("Content-Encoding", "")

        if "text/event-stream" in content_type:
            self._forward_sse_response(resp, resp_headers, model_name, content_encoding, track_usage)
        else:
            self._forward_json_response(resp, resp_headers, model_name, content_encoding, track_usage)

        conn.close()

    def _emit_realtime_cost_budget(
        self,
        request_no: int,
        model_name: str | None,
        req_input: int,
        req_output: int,
        req_cache_create: int,
        req_cache_read: int,
    ) -> None:
        if not self.budget.enabled:
            return
        req_cost = _estimate_request_cost_usd(
            model_name,
            req_input,
            req_output,
            req_cache_create,
            req_cache_read,
        )
        _session_spent, _total_spent, remaining = self.budget.add_cost(req_cost)
        _persist_budget(self.budget)
        self._print_status_line(f"[kbclaude] req#{request_no} remaining~${remaining:.4f}")

    def _emit_live_request_progress(
        self,
        model_name: str | None,
        started_at: float,
        req_input: int,
        req_output: int,
        req_cache_create: int,
        req_cache_read: int,
    ) -> None:
        if not self.realtime_usage_enabled:
            return
        if not self.budget.enabled:
            return
        elapsed = max(0.0, time.monotonic() - started_at)
        req_cost = _estimate_request_cost_usd(
            model_name,
            req_input,
            req_output,
            req_cache_create,
            req_cache_read,
        )
        live_total = self.budget.total_spent_usd + req_cost
        remaining = max(0.0, self.budget.budget_usd - live_total)
        self._print_status_line(f"[kbclaude] live t+{elapsed:.1f}s remaining~${remaining:.4f}")

    def _forward_sse_response(
        self,
        resp: http.client.HTTPResponse,
        resp_headers: list,
        model_name: str | None,
        content_encoding: str,
        track_usage: bool,
    ) -> None:
        self.send_response(resp.status, resp.reason)
        for name, value in resp_headers:
            self.send_header(name, value)
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()

        req_input = req_output = req_cache_create = req_cache_read = 0
        started_at = time.monotonic()
        last_live_emit = 0.0
        decoder = _make_sse_decoder(content_encoding)
        decoded_buf = b""

        def _consume_decoded(decoded_chunk: bytes) -> None:
            nonlocal decoded_buf, req_input, req_output, req_cache_create, req_cache_read
            if not decoded_chunk:
                return
            decoded_buf += decoded_chunk
            while b"\n" in decoded_buf:
                one_line, decoded_buf = decoded_buf.split(b"\n", 1)
                tokens = _extract_usage_from_sse_line(one_line + b"\n")
                req_input += tokens.get("input_tokens", 0)
                req_output += tokens.get("output_tokens", 0)
                req_cache_create += tokens.get("cache_creation_tokens", 0)
                req_cache_read += tokens.get("cache_read_tokens", 0)

        try:
            while True:
                line = resp.readline()
                if not line:
                    break

                prev_totals = (req_input, req_output, req_cache_create, req_cache_read)

                if decoder is not None:
                    try:
                        _consume_decoded(decoder.decompress(line))
                    except Exception:
                        pass
                else:
                    _consume_decoded(line)

                chunk = hex(len(line))[2:].encode("ascii") + b"\r\n" + line + b"\r\n"
                self.wfile.write(chunk)
                self.wfile.flush()

                if self.realtime_usage_enabled and (
                    req_input,
                    req_output,
                    req_cache_create,
                    req_cache_read,
                ) != prev_totals:
                    now = time.monotonic()
                    if now - last_live_emit >= self.realtime_usage_interval_sec:
                        self._emit_live_request_progress(
                            model_name,
                            started_at,
                            req_input,
                            req_output,
                            req_cache_create,
                            req_cache_read,
                        )
                        last_live_emit = now
        except (BrokenPipeError, ConnectionResetError):
            pass

        try:
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass

        if decoder is not None:
            try:
                _consume_decoded(decoder.flush())
            except Exception:
                pass

        if decoded_buf:
            tokens = _extract_usage_from_sse_line(decoded_buf)
            req_input += tokens.get("input_tokens", 0)
            req_output += tokens.get("output_tokens", 0)
            req_cache_create += tokens.get("cache_creation_tokens", 0)
            req_cache_read += tokens.get("cache_read_tokens", 0)

        if self.realtime_usage_enabled and (req_input or req_output or req_cache_create or req_cache_read):
            self._emit_live_request_progress(
                model_name,
                started_at,
                req_input,
                req_output,
                req_cache_create,
                req_cache_read,
            )

        if track_usage:
            request_no = self.usage.add(req_input, req_output, req_cache_create, req_cache_read)
            if req_input or req_output or req_cache_create or req_cache_read:
                self._emit_realtime_cost_budget(
                    request_no,
                    model_name,
                    req_input,
                    req_output,
                    req_cache_create,
                    req_cache_read,
                )
            else:
                self._print_status_line(
                    f"[kbclaude] req#{request_no} observed, but usage fields were missing in API response"
                )

    def _forward_json_response(
        self,
        resp: http.client.HTTPResponse,
        resp_headers: list,
        model_name: str | None,
        content_encoding: str,
        track_usage: bool,
    ) -> None:
        body = resp.read()
        parse_body = _maybe_decode_for_parsing(body, content_encoding)
        tokens = _extract_usage_from_json_body(parse_body)

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

        if track_usage:
            request_no = self.usage.add(
                tokens.get("input_tokens", 0),
                tokens.get("output_tokens", 0),
                tokens.get("cache_creation_tokens", 0),
                tokens.get("cache_read_tokens", 0),
            )
            if any(tokens.get(k, 0) for k in (
                "input_tokens",
                "output_tokens",
                "cache_creation_tokens",
                "cache_read_tokens",
            )):
                self._emit_realtime_cost_budget(
                    request_no,
                    model_name,
                    tokens.get("input_tokens", 0),
                    tokens.get("output_tokens", 0),
                    tokens.get("cache_creation_tokens", 0),
                    tokens.get("cache_read_tokens", 0),
                )
            else:
                self._print_status_line(
                    f"[kbclaude] req#{request_no} observed, but usage fields were missing in API response"
                )


def _make_server(
    usage: TokenUsage,
    snapshot_path: Path,
    budget: BudgetTracker,
    realtime_usage_enabled: bool,
    realtime_usage_interval_sec: float,
) -> socketserver.ThreadingTCPServer:
    lock = threading.Lock()
    inject_state = {"done": False}

    class _Handler(ProxyHandler):
        pass
    _Handler.usage = usage
    _Handler.snapshot_path = snapshot_path
    _Handler.budget = budget
    _Handler.realtime_usage_enabled = realtime_usage_enabled
    _Handler.realtime_usage_interval_sec = realtime_usage_interval_sec
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


def _budget_log_path() -> Path:
    return Path.home() / ".kbgen" / "budget.json"


def _load_budget_from_env() -> BudgetTracker:
    raw_budget = os.environ.get("KBGEN_BUDGET_USD", "").strip()
    if not raw_budget:
        return BudgetTracker()
    try:
        budget_usd = float(raw_budget)
    except ValueError:
        print(f"[kbclaude] invalid KBGEN_BUDGET_USD={raw_budget!r}, budget tracking disabled", file=sys.stderr)
        return BudgetTracker()
    if budget_usd <= 0:
        return BudgetTracker()

    persist = os.environ.get("KBGEN_BUDGET_PERSIST", "1").strip().lower() not in {"0", "false", "no"}
    tracker = BudgetTracker(budget_usd=budget_usd, persist=persist)
    if persist:
        try:
            payload = json.loads(_budget_log_path().read_text(encoding="utf-8"))
            if isinstance(payload, dict):
                if float(payload.get("budget_usd", budget_usd)) == budget_usd:
                    tracker.total_spent_usd = float(payload.get("total_spent_usd", 0.0))
        except Exception:
            pass
    return tracker


def _persist_budget(budget: BudgetTracker) -> None:
    if not budget.enabled or not budget.persist:
        return
    try:
        path = _budget_log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "budget_usd": budget.budget_usd,
            "total_spent_usd": budget.total_spent_usd,
            "updated_at": datetime.datetime.now().isoformat(timespec="seconds"),
        }
        path.write_text(json.dumps(payload, ensure_ascii=True), encoding="utf-8")
    except Exception:
        pass


def _persist_session(usage: "TokenUsage", elapsed_sec: float, cwd: Path, quality: dict | None = None) -> None:
    """Append one session record to ~/.kbgen/sessions.jsonl (best-effort)."""
    try:
        log_path = _sessions_log_path()
        log_path.parent.mkdir(parents=True, exist_ok=True)
        snapshot_path = cwd / ".ai" / "snapshot.kb"
        snapshot_present = snapshot_path.exists()

        snapshot_tokens: int | None = None
        if snapshot_present:
            try:
                raw = snapshot_path.read_text(encoding="utf-8")
                from kbgen.parsing import estimate_tokens
                snapshot_tokens = estimate_tokens(raw)
            except Exception:
                pass

        q = quality or {}
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
            "snapshot_tokens": snapshot_tokens,
            "snapshot_coverage_pct": q.get("coverage_pct"),
            "snapshot_staleness_pct": q.get("staleness_pct"),
            "snapshot_quality_score": q.get("quality_score"),
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
    budget = _load_budget_from_env()
    realtime_usage_enabled, realtime_usage_interval_sec = _load_realtime_usage_settings()
    if budget.enabled:
        remaining = max(0.0, budget.budget_usd - budget.total_spent_usd)
        mode = "persistent" if budget.persist else "session"
        print(
            f"[kbclaude] budget mode={mode} total=${budget.budget_usd:.4f} remaining=${remaining:.4f}",
            file=sys.stderr,
        )
    if realtime_usage_enabled:
        print(
            f"[kbclaude] realtime usage enabled (interval={realtime_usage_interval_sec:.1f}s)",
            file=sys.stderr,
        )
    server = _make_server(
        usage,
        snapshot_path,
        budget,
        realtime_usage_enabled,
        realtime_usage_interval_sec,
    )
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
        quality = compute_quality(cwd)
        try:
            from kbgen.gain import _load_sessions, _total_input
            all_sessions = _load_sessions()
            no_snap = [s for s in all_sessions if not s.get("snapshot")]
            avg_no_snap: float | None = (
                sum(_total_input(s) for s in no_snap) / len(no_snap)
                if no_snap else None
            )
        except Exception:
            avg_no_snap = None
        _persist_session(usage, elapsed, cwd, quality=quality)
        print(f"\n{usage.format_summary(elapsed, quality=quality, avg_no_snap_input=avg_no_snap)}", file=sys.stderr)
        if budget.enabled:
            _persist_budget(budget)
            print(f"\n{budget.format_summary()}", file=sys.stderr)

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
