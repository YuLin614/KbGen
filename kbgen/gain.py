from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def _sessions_log_path() -> Path:
    return Path.home() / ".kbgen" / "sessions.jsonl"


def _load_sessions() -> list[dict[str, Any]]:
    log_path = _sessions_log_path()
    if not log_path.exists():
        return []
    sessions: list[dict[str, Any]] = []
    for line in log_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            sessions.append(json.loads(line))
        except Exception:
            pass
    return sessions


def _fmt_duration(seconds: float) -> str:
    s = int(seconds)
    if s < 60:
        return f"{s}s"
    return f"{s // 60}m {s % 60:02d}s"


def _fmt_num(n: int) -> str:
    return f"{n:,}"


def _total_input(rec: dict[str, Any]) -> int:
    return rec.get("input_tokens", 0) + rec.get("cache_write_tokens", 0) + rec.get("cache_read_tokens", 0)


def show_gain(n_recent: int = 10, show_history: bool = False) -> None:
    sessions = _load_sessions()
    if not sessions:
        print("No sessions recorded yet. Run kbclaude (or kbgen claude) to start tracking.")
        print(f"Session log: {_sessions_log_path()}")
        return

    total = len(sessions)
    snap_sessions = [s for s in sessions if s.get("snapshot")]
    no_snap_sessions = [s for s in sessions if not s.get("snapshot")]

    # --- summary header ---
    print(f"--- kbgen gain ({total} session{'s' if total != 1 else ''} tracked) ---")
    print(f"Log: {_sessions_log_path()}")
    print()

    # --- snapshot vs no-snapshot comparison ---
    if snap_sessions and no_snap_sessions:
        avg_input_snap = sum(_total_input(s) for s in snap_sessions) / len(snap_sessions)
        avg_input_nosnap = sum(_total_input(s) for s in no_snap_sessions) / len(no_snap_sessions)
        if avg_input_nosnap > 0:
            savings_pct = (1 - avg_input_snap / avg_input_nosnap) * 100
            direction = "fewer" if savings_pct >= 0 else "more"
            print(f"Snapshot effect ({len(snap_sessions)} with / {len(no_snap_sessions)} without):")
            print(f"  avg total input  with snapshot : {_fmt_num(int(avg_input_snap))} tokens")
            print(f"  avg total input  no  snapshot  : {_fmt_num(int(avg_input_nosnap))} tokens")
            print(f"  estimated savings               : {abs(savings_pct):.0f}% {direction} tokens")
            print()
    elif snap_sessions:
        print(f"All {len(snap_sessions)} session(s) used a snapshot. Run without snapshot to see comparison.")
        print()
    else:
        print(f"No sessions with snapshot yet. Run kbgen scan then kbclaude to see savings.")
        print()

    # --- recent sessions table ---
    recent = sessions[-n_recent:]
    if show_history:
        recent = sessions

    label = "All sessions" if show_history else f"Recent sessions (last {len(recent)})"
    print(f"{label}:")
    print(f"  {'Date':>10}  {'Duration':>8}  {'Snap':>4}  {'Req':>4}  {'Input':>9}  {'Output':>9}  {'CacheR':>9}  {'CacheW':>8}")
    print(f"  {'-'*10}  {'-'*8}  {'-'*4}  {'-'*4}  {'-'*9}  {'-'*9}  {'-'*9}  {'-'*8}")
    for s in recent:
        ts = s.get("ts", "")[:10]
        dur = _fmt_duration(s.get("duration_s", 0))
        snap = "yes" if s.get("snapshot") else "no"
        req = s.get("requests", 0)
        inp = s.get("input_tokens", 0)
        out = s.get("output_tokens", 0)
        cr = s.get("cache_read_tokens", 0)
        cw = s.get("cache_write_tokens", 0)
        print(f"  {ts:>10}  {dur:>8}  {snap:>4}  {req:>4}  {_fmt_num(inp):>9}  {_fmt_num(out):>9}  {_fmt_num(cr):>9}  {_fmt_num(cw):>8}")

    # --- totals ---
    print()
    total_inp = sum(s.get("input_tokens", 0) for s in sessions)
    total_out = sum(s.get("output_tokens", 0) for s in sessions)
    total_cr = sum(s.get("cache_read_tokens", 0) for s in sessions)
    total_cw = sum(s.get("cache_write_tokens", 0) for s in sessions)
    total_req = sum(s.get("requests", 0) for s in sessions)
    total_dur = sum(s.get("duration_s", 0) for s in sessions)
    print(f"Totals ({total} sessions, {_fmt_duration(total_dur)} total):")
    print(f"  Requests         : {_fmt_num(total_req)}")
    print(f"  Input tokens     : {_fmt_num(total_inp)}  (uncached)")
    print(f"  Output tokens    : {_fmt_num(total_out)}")
    print(f"  Cache read       : {_fmt_num(total_cr)}")
    print(f"  Cache write      : {_fmt_num(total_cw)}")
    print(f"  Total input      : {_fmt_num(total_inp + total_cr + total_cw)}  (all sources)")
