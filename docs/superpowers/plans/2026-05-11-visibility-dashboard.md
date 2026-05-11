# Visibility Dashboard Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add snapshot quality scoring, session savings feedback, and an HTML dashboard to make kbGen's value measurable.

**Architecture:** Three new/modified layers: `quality.py` computes coverage+staleness from the snapshot and git, `report.py` generates a self-contained HTML file, and `claude_wrapper.py`+`gain.py`+`cli.py` are enhanced to surface these signals in the terminal and via a new `dashboard` subcommand.

**Tech Stack:** Python 3.10+ stdlib only (subprocess for git, webbrowser for open, f-strings for HTML/SVG). No new dependencies.

---

## File Map

| Action | Path | Responsibility |
|--------|------|----------------|
| Create | `kbgen/quality.py` | Coverage %, staleness %, composite score, grade |
| Create | `kbgen/report.py` | Self-contained HTML+SVG+JS dashboard generator |
| Create | `tests/test_quality.py` | Unit tests for quality module |
| Create | `tests/test_report.py` | Unit tests for HTML generator |
| Modify | `kbgen/constants.py` | Add `QUALITY_GRADES`, `QUALITY_WEIGHTS` |
| Modify | `kbgen/claude_wrapper.py` | Compute+persist quality fields, enhance session summary |
| Modify | `kbgen/gain.py` | Add `show_dashboard()`, sparkline trend, A/B stats |
| Modify | `kbgen/cli.py` | Add `dashboard` subcommand |

---

## Task 1: Add quality grade constants to `constants.py`

**Files:**
- Modify: `kbgen/constants.py`

- [ ] **Step 1: Add constants**

Open `kbgen/constants.py` and append at the end:

```python
# Quality scoring
QUALITY_WEIGHTS = {"coverage": 0.6, "freshness": 0.4}
QUALITY_GRADES = [
    (85, "A"),
    (70, "B"),
    (50, "C"),
    (0,  "D"),
]
```

- [ ] **Step 2: Commit**

```bash
git add kbgen/constants.py
git commit -m "feat: add quality grade constants"
```

---

## Task 2: Create `kbgen/quality.py` (TDD)

**Files:**
- Create: `tests/__init__.py`
- Create: `tests/test_quality.py`
- Create: `kbgen/quality.py`

- [ ] **Step 1: Create tests directory**

```bash
mkdir tests
touch tests/__init__.py
```

- [ ] **Step 2: Write failing tests**

Create `tests/test_quality.py`:

```python
from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from kbgen.quality import compute_quality, _extract_snapshot_paths, _grade


def test_grade_boundaries():
    assert _grade(90) == "A"
    assert _grade(85) == "A"
    assert _grade(84) == "B"
    assert _grade(70) == "B"
    assert _grade(69) == "C"
    assert _grade(50) == "C"
    assert _grade(49) == "D"
    assert _grade(0)  == "D"


def test_extract_snapshot_paths_empty():
    snapshot = {"m": {}}
    assert _extract_snapshot_paths(snapshot) == set()


def test_extract_snapshot_paths_uses_p_field():
    snapshot = {
        "m": {
            "api": {"p": ["api/routes.py", "api/handlers.py"]},
            "auth": {"p": ["auth/middleware.py"]},
        }
    }
    result = _extract_snapshot_paths(snapshot)
    assert result == {"api/routes.py", "api/handlers.py", "auth/middleware.py"}


def test_extract_snapshot_paths_missing_p_field():
    snapshot = {
        "m": {
            "api": {"r": ["routing"], "s": "routes"},  # no "p" key
        }
    }
    assert _extract_snapshot_paths(snapshot) == set()


def test_compute_quality_no_snapshot(tmp_path):
    result = compute_quality(tmp_path)
    assert result["available"] is False
    assert result["coverage_pct"] is None
    assert result["staleness_pct"] is None
    assert result["quality_score"] is None
    assert result["grade"] is None


def test_compute_quality_with_snapshot(tmp_path):
    # Create source files
    (tmp_path / "api").mkdir()
    (tmp_path / "api" / "routes.py").write_text("def get(): pass")
    (tmp_path / "api" / "handlers.py").write_text("def handle(): pass")
    (tmp_path / "auth").mkdir()
    (tmp_path / "auth" / "middleware.py").write_text("def check(): pass")

    # Create snapshot referencing 2 of 3 files
    ai_dir = tmp_path / ".ai"
    ai_dir.mkdir()
    snapshot = {
        "m": {
            "api": {"p": ["api/routes.py", "api/handlers.py"]},
        }
    }
    (ai_dir / "snapshot.kb").write_text(json.dumps(snapshot))
    (ai_dir / "meta.json").write_text(json.dumps({
        "generated_at": "2026-01-01T00:00:00",
        "tool_version": "kbgen@0.1",
        "confidence": "high",
    }))

    # Mock git to return no changed files
    with patch("kbgen.quality._git_changed_since", return_value=set()):
        result = compute_quality(tmp_path)

    assert result["available"] is True
    assert result["total_files"] == 3
    assert result["covered_files"] == 2
    assert abs(result["coverage_pct"] - 66.7) < 1.0
    assert result["staleness_pct"] == 0.0
    assert result["stale_files"] == 0
    # score = 66.7 * 0.6 + 100 * 0.4 = 40.0 + 40.0 = 80.0
    assert abs(result["quality_score"] - 80.0) < 1.0
    assert result["grade"] == "B"


def test_compute_quality_staleness(tmp_path):
    (tmp_path / "api").mkdir()
    (tmp_path / "api" / "routes.py").write_text("def get(): pass")
    (tmp_path / "api" / "handlers.py").write_text("def handle(): pass")

    ai_dir = tmp_path / ".ai"
    ai_dir.mkdir()
    snapshot = {"m": {"api": {"p": ["api/routes.py", "api/handlers.py"]}}}
    (ai_dir / "snapshot.kb").write_text(json.dumps(snapshot))
    (ai_dir / "meta.json").write_text(json.dumps({"generated_at": "2026-01-01T00:00:00"}))

    # 1 of 2 snapshot files changed
    with patch("kbgen.quality._git_changed_since", return_value={"api/routes.py"}):
        result = compute_quality(tmp_path)

    assert result["stale_files"] == 1
    assert abs(result["staleness_pct"] - 50.0) < 1.0
```

- [ ] **Step 3: Run tests to verify they fail**

```bash
python -m pytest tests/test_quality.py -v 2>&1 | head -30
```

Expected: `ModuleNotFoundError: No module named 'kbgen.quality'`

- [ ] **Step 4: Implement `kbgen/quality.py`**

Create `kbgen/quality.py`:

```python
from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

from kbgen.constants import QUALITY_GRADES, QUALITY_WEIGHTS, SUPPORTED_EXTENSIONS
from kbgen.parsing import collect_source_files


def _grade(score: float) -> str:
    for threshold, letter in QUALITY_GRADES:
        if score >= threshold:
            return letter
    return "D"


def _extract_snapshot_paths(snapshot: dict[str, Any]) -> set[str]:
    paths: set[str] = set()
    for module_data in snapshot.get("m", {}).values():
        for p in module_data.get("p", []):
            if isinstance(p, str):
                paths.add(p)
    return paths


def _git_changed_since(root: Path, since_ts: str) -> set[str]:
    """Return relative posix paths of source files changed since since_ts."""
    try:
        result = subprocess.run(
            ["git", "log", f"--since={since_ts}", "--name-only", "--format="],
            cwd=str(root),
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode != 0:
            return set()
        changed: set[str] = set()
        for line in result.stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            if Path(line).suffix.lower() in SUPPORTED_EXTENSIONS:
                changed.add(line)
        return changed
    except Exception:
        return set()


def _mtime_changed_since(root: Path, since_ts: str, snapshot_paths: set[str]) -> set[str]:
    """Fallback: compare file mtimes against since_ts ISO string."""
    try:
        from datetime import datetime, timezone
        cutoff = datetime.fromisoformat(since_ts.replace("Z", "+00:00")).timestamp()
    except Exception:
        return set()
    changed: set[str] = set()
    for rel in snapshot_paths:
        abs_path = root / rel
        try:
            if abs_path.stat().st_mtime > cutoff:
                changed.add(rel)
        except OSError:
            pass
    return changed


def compute_quality(root: Path) -> dict[str, Any]:
    """Compute snapshot quality metrics. Returns dict with available=False if no snapshot."""
    snapshot_path = root / ".ai" / "snapshot.kb"
    meta_path = root / ".ai" / "meta.json"

    if not snapshot_path.exists():
        return {
            "available": False,
            "coverage_pct": None,
            "staleness_pct": None,
            "quality_score": None,
            "grade": None,
            "total_files": None,
            "covered_files": None,
            "stale_files": None,
            "generated_at": None,
        }

    try:
        snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
    except Exception:
        return {"available": False, "coverage_pct": None, "staleness_pct": None,
                "quality_score": None, "grade": None, "total_files": None,
                "covered_files": None, "stale_files": None, "generated_at": None}

    generated_at: str | None = None
    if meta_path.exists():
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            generated_at = meta.get("generated_at")
        except Exception:
            pass

    snapshot_paths = _extract_snapshot_paths(snapshot)
    all_files = collect_source_files(root)
    total_files = len(all_files)

    all_file_rels = {f.relative_to(root).as_posix() for f in all_files}
    covered = snapshot_paths & all_file_rels
    covered_files = len(covered)
    coverage_pct = (covered_files / total_files * 100) if total_files > 0 else 0.0

    stale_files = 0
    staleness_pct = 0.0
    if generated_at and snapshot_paths:
        changed = _git_changed_since(root, generated_at)
        if not changed and generated_at:
            changed = _mtime_changed_since(root, generated_at, snapshot_paths)
        stale = changed & snapshot_paths
        stale_files = len(stale)
        staleness_pct = (stale_files / len(snapshot_paths) * 100) if snapshot_paths else 0.0

    freshness_pct = 100.0 - staleness_pct
    quality_score = (
        coverage_pct * QUALITY_WEIGHTS["coverage"]
        + freshness_pct * QUALITY_WEIGHTS["freshness"]
    )
    grade = _grade(quality_score)

    return {
        "available": True,
        "coverage_pct": round(coverage_pct, 1),
        "staleness_pct": round(staleness_pct, 1),
        "quality_score": round(quality_score, 1),
        "grade": grade,
        "total_files": total_files,
        "covered_files": covered_files,
        "stale_files": stale_files,
        "generated_at": generated_at,
    }


def format_quality_terminal(q: dict[str, Any], indent: str = "  ") -> str:
    """Render quality as terminal text block."""
    if not q.get("available"):
        return f"{indent}Snapshot quality: n/a (no snapshot found)"
    score = q["quality_score"]
    grade = q["grade"]
    cov = q["coverage_pct"]
    fresh = 100.0 - q["staleness_pct"]
    cov_bar = _bar(cov)
    fresh_bar = _bar(fresh)
    covered = q["covered_files"]
    total = q["total_files"]
    stale = q["stale_files"]
    lines = [
        f"{indent}Snapshot quality: {grade} ({score:.0f}/100)",
        f"{indent}  Coverage:  {cov:5.1f}%  {cov_bar}  ({covered}/{total} files)",
        f"{indent}  Freshness: {fresh:5.1f}%  {fresh_bar}  ({stale} files changed since scan)",
    ]
    if stale > 0:
        lines.append(f"{indent}  → Run `kbgen update` to refresh")
    return "\n".join(lines)


def _bar(pct: float, width: int = 10) -> str:
    filled = round(pct / 100 * width)
    return "█" * filled + "░" * (width - filled)
```

- [ ] **Step 5: Run tests to verify they pass**

```bash
python -m pytest tests/test_quality.py -v
```

Expected: all 7 tests PASS.

- [ ] **Step 6: Commit**

```bash
git add kbgen/quality.py tests/__init__.py tests/test_quality.py
git commit -m "feat: add quality.py with coverage and staleness scoring"
```

---

## Task 3: Enhance `_persist_session` in `claude_wrapper.py`

**Files:**
- Modify: `kbgen/claude_wrapper.py`

- [ ] **Step 1: Add import at top of `claude_wrapper.py`**

Find the block of imports (lines 1–17). Add after the existing imports:

```python
from kbgen.quality import compute_quality
```

- [ ] **Step 2: Replace `_persist_session` function (lines 663–683)**

Replace the entire `_persist_session` function with:

```python
def _persist_session(usage: "TokenUsage", elapsed_sec: float, cwd: Path) -> None:
    """Append one session record to ~/.kbgen/sessions.jsonl (best-effort)."""
    try:
        log_path = _sessions_log_path()
        log_path.parent.mkdir(parents=True, exist_ok=True)
        snapshot_path = cwd / ".ai" / "snapshot.kb"
        snapshot_present = snapshot_path.exists()

        quality = compute_quality(cwd)

        snapshot_tokens: int | None = None
        if snapshot_present:
            try:
                raw = snapshot_path.read_text(encoding="utf-8")
                from kbgen.parsing import estimate_tokens
                snapshot_tokens = estimate_tokens(raw)
            except Exception:
                pass

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
            "snapshot_coverage_pct": quality.get("coverage_pct"),
            "snapshot_staleness_pct": quality.get("staleness_pct"),
            "snapshot_quality_score": quality.get("quality_score"),
        }
        with log_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record) + "\n")
    except Exception:
        pass  # never break the user's session over logging
```

- [ ] **Step 3: Enhance `format_summary` on `TokenUsage` to include quality line**

The current `format_summary` (lines 81–94) takes only `elapsed_sec`. We need to add optional quality data and savings estimate.

Replace the `format_summary` method on `TokenUsage` with:

```python
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
        lines.append(f"  Snapshot quality  : {grade} ({score:.0f}/100)" +
                     (f"  [{stale} files stale — run `kbgen update`]" if stale > 0 else ""))
    lines.append("--------------------------------")
    return "\n".join(lines)
```

- [ ] **Step 4: Update `run_claude_with_proxy` to pass quality+savings into format_summary**

In `run_claude_with_proxy`, the `finally` block (around line 780–787) currently calls:
```python
print(f"\n{usage.format_summary(elapsed)}", file=sys.stderr)
```

Replace with:

```python
quality = compute_quality(cwd)
# compute avg no-snapshot input for savings estimate
try:
    from kbgen.gain import _load_sessions, _total_input
    all_sessions = _load_sessions()
    no_snap = [s for s in all_sessions if not s.get("snapshot")]
    avg_no_snap = (
        sum(_total_input(s) for s in no_snap) / len(no_snap)
        if no_snap else None
    )
except Exception:
    avg_no_snap = None
print(f"\n{usage.format_summary(elapsed, quality=quality, avg_no_snap_input=avg_no_snap)}", file=sys.stderr)
```

- [ ] **Step 5: Verify import works**

```bash
python -c "from kbgen.claude_wrapper import run_claude_with_proxy; print('ok')"
```

Expected: `ok`

- [ ] **Step 6: Commit**

```bash
git add kbgen/claude_wrapper.py
git commit -m "feat: persist quality fields in session log and show in summary"
```

---

## Task 4: Create `kbgen/report.py` (TDD)

**Files:**
- Create: `tests/test_report.py`
- Create: `kbgen/report.py`

- [ ] **Step 1: Write failing tests**

Create `tests/test_report.py`:

```python
from __future__ import annotations

from kbgen.report import generate_html, _sparkline, _svg_line_chart


def test_sparkline_empty():
    assert _sparkline([]) == ""


def test_sparkline_single():
    result = _sparkline([1000])
    assert len(result) == 1
    assert result in "▁▂▃▄▅▆▇█"


def test_sparkline_ascending():
    result = _sparkline([100, 200, 300, 400])
    assert len(result) == 4
    # last char should be higher than first
    bars = "▁▂▃▄▅▆▇█"
    assert bars.index(result[-1]) >= bars.index(result[0])


def test_svg_line_chart_empty():
    svg = _svg_line_chart([], [], width=400, height=100)
    assert "<svg" in svg
    assert "</svg>" in svg


def test_svg_line_chart_two_series():
    snap_vals = [1000, 900, 800]
    no_snap_vals = [1500, 1400, 1600]
    svg = _svg_line_chart(snap_vals, no_snap_vals, width=400, height=100)
    assert "<polyline" in svg
    assert 'stroke="' in svg


def test_generate_html_minimal():
    html = generate_html(sessions=[], quality=None, project_name="test")
    assert "<!DOCTYPE html>" in html
    assert "kbGen Dashboard" in html
    assert "test" in html


def test_generate_html_with_sessions():
    sessions = [
        {"ts": "2026-05-01T10:00:00", "snapshot": True, "input_tokens": 1000,
         "output_tokens": 200, "cache_read_tokens": 5000, "cache_write_tokens": 0,
         "duration_s": 60.0, "requests": 3, "snapshot_quality_score": 80},
        {"ts": "2026-05-02T10:00:00", "snapshot": False, "input_tokens": 1500,
         "output_tokens": 300, "cache_read_tokens": 0, "cache_write_tokens": 0,
         "duration_s": 90.0, "requests": 4, "snapshot_quality_score": None},
    ]
    quality = {
        "available": True, "coverage_pct": 82.3, "staleness_pct": 5.0,
        "quality_score": 78.9, "grade": "B", "total_files": 100,
        "covered_files": 82, "stale_files": 3, "generated_at": "2026-05-01T00:00:00"
    }
    html = generate_html(sessions=sessions, quality=quality, project_name="myapp")
    assert "myapp" in html
    assert "82.3" in html  # coverage_pct
    assert "78.9" in html or "79" in html  # quality score
    assert "2026-05-01" in html
    assert "<table" in html
    assert "<polyline" in html  # SVG chart
```

- [ ] **Step 2: Run tests to see failures**

```bash
python -m pytest tests/test_report.py -v 2>&1 | head -20
```

Expected: `ModuleNotFoundError: No module named 'kbgen.report'`

- [ ] **Step 3: Implement `kbgen/report.py`**

Create `kbgen/report.py`:

```python
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

_BARS = "▁▂▃▄▅▆▇█"


def _sparkline(values: list[float]) -> str:
    if not values:
        return ""
    lo, hi = min(values), max(values)
    rng = hi - lo if hi != lo else 1
    result = []
    for v in values:
        idx = int((v - lo) / rng * (len(_BARS) - 1))
        result.append(_BARS[idx])
    return "".join(result)


def _svg_line_chart(
    snap_vals: list[float],
    no_snap_vals: list[float],
    width: int = 600,
    height: int = 120,
) -> str:
    pad = 10
    all_vals = snap_vals + no_snap_vals
    if not all_vals:
        return f'<svg width="{width}" height="{height}" xmlns="http://www.w3.org/2000/svg"></svg>'

    lo = min(all_vals)
    hi = max(all_vals)
    rng = hi - lo if hi != lo else 1

    def _points(vals: list[float]) -> str:
        if not vals:
            return ""
        n = len(vals)
        pts = []
        for i, v in enumerate(vals):
            x = pad + i / max(n - 1, 1) * (width - 2 * pad)
            y = pad + (1 - (v - lo) / rng) * (height - 2 * pad)
            pts.append(f"{x:.1f},{y:.1f}")
        return " ".join(pts)

    lines = []
    if snap_vals:
        pts = _points(snap_vals)
        lines.append(
            f'<polyline points="{pts}" fill="none" stroke="#4ade80" stroke-width="2"/>'
        )
    if no_snap_vals:
        pts = _points(no_snap_vals)
        lines.append(
            f'<polyline points="{pts}" fill="none" stroke="#f87171" stroke-width="2" stroke-dasharray="4 2"/>'
        )

    inner = "\n  ".join(lines)
    return (
        f'<svg width="{width}" height="{height}" xmlns="http://www.w3.org/2000/svg" '
        f'style="background:#1e1e2e;border-radius:6px">\n  {inner}\n</svg>'
    )


def _total_input(rec: dict[str, Any]) -> int:
    return rec.get("input_tokens", 0) + rec.get("cache_write_tokens", 0) + rec.get("cache_read_tokens", 0)


def generate_html(
    sessions: list[dict[str, Any]],
    quality: dict[str, Any] | None,
    project_name: str,
) -> str:
    snap_sessions = [s for s in sessions if s.get("snapshot")]
    no_snap_sessions = [s for s in sessions if not s.get("snapshot")]

    avg_snap = (sum(_total_input(s) for s in snap_sessions) / len(snap_sessions)) if snap_sessions else 0
    avg_no_snap = (sum(_total_input(s) for s in no_snap_sessions) / len(no_snap_sessions)) if no_snap_sessions else 0
    saving_pct = ((1 - avg_snap / avg_no_snap) * 100) if avg_no_snap > 0 and avg_snap > 0 else None

    snap_series = [_total_input(s) for s in snap_sessions[-20:]]
    no_snap_series = [_total_input(s) for s in no_snap_sessions[-20:]]
    chart_svg = _svg_line_chart(snap_series, no_snap_series)

    q = quality or {}
    grade = q.get("grade", "n/a")
    score = q.get("quality_score")
    score_str = f"{score:.1f}" if score is not None else "n/a"
    cov = q.get("coverage_pct")
    cov_str = f"{cov:.1f}" if cov is not None else "n/a"
    stale = q.get("stale_files", 0)
    fresh = (100.0 - q["staleness_pct"]) if q.get("staleness_pct") is not None else None
    fresh_str = f"{fresh:.1f}" if fresh is not None else "n/a"

    saving_str = f"{saving_pct:.1f}%" if saving_pct is not None else "n/a"

    def _cov_bar(pct: float | None, width: int = 120) -> str:
        if pct is None:
            return ""
        filled = int(pct / 100 * width)
        return (
            f'<div style="background:#1e1e2e;border-radius:4px;height:12px;width:{width}px">'
            f'<div style="background:#4ade80;height:12px;width:{filled}px;border-radius:4px"></div>'
            f'</div>'
        )

    rows = []
    for s in reversed(sessions[-50:]):
        ts = s.get("ts", "")[:16]
        snap_flag = "✓" if s.get("snapshot") else "–"
        inp = f'{_total_input(s):,}'
        out = f'{s.get("output_tokens", 0):,}'
        cr = f'{s.get("cache_read_tokens", 0):,}'
        dur = f'{s.get("duration_s", 0):.0f}s'
        qs = s.get("snapshot_quality_score")
        qs_str = f"{qs:.0f}" if qs is not None else "–"
        rows.append(
            f"<tr><td>{ts}</td><td>{snap_flag}</td><td>{inp}</td>"
            f"<td>{out}</td><td>{cr}</td><td>{dur}</td><td>{qs_str}</td></tr>"
        )
    rows_html = "\n".join(rows)

    session_data = json.dumps([
        {"ts": s.get("ts", ""), "snap": s.get("snapshot", False),
         "total_input": _total_input(s)}
        for s in sessions
    ])

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>kbGen Dashboard — {project_name}</title>
<style>
  body {{ font-family: monospace; background: #0f0f1a; color: #cdd6f4; margin: 0; padding: 20px; }}
  h1 {{ color: #cba6f7; margin-bottom: 4px; }}
  .sub {{ color: #6c7086; font-size: 0.85em; margin-bottom: 24px; }}
  .cards {{ display: flex; gap: 16px; margin-bottom: 24px; flex-wrap: wrap; }}
  .card {{ background: #1e1e2e; border-radius: 8px; padding: 16px 20px; min-width: 120px; }}
  .card-label {{ color: #6c7086; font-size: 0.75em; text-transform: uppercase; }}
  .card-value {{ font-size: 1.8em; color: #cba6f7; font-weight: bold; }}
  .section {{ margin-bottom: 28px; }}
  .section h2 {{ color: #89b4fa; font-size: 1em; border-bottom: 1px solid #313244; padding-bottom: 4px; }}
  table {{ width: 100%; border-collapse: collapse; font-size: 0.85em; }}
  th {{ text-align: left; color: #6c7086; padding: 4px 8px; border-bottom: 1px solid #313244; cursor: pointer; }}
  td {{ padding: 4px 8px; border-bottom: 1px solid #1e1e2e; }}
  tr:hover {{ background: #1e1e2e; }}
  .legend {{ font-size: 0.8em; color: #6c7086; margin-top: 6px; }}
  .legend span.snap {{ color: #4ade80; }}
  .legend span.nosnap {{ color: #f87171; }}
  .q-row {{ display: flex; align-items: center; gap: 10px; margin: 6px 0; font-size: 0.9em; }}
  .q-label {{ width: 80px; color: #6c7086; }}
  .q-val {{ width: 50px; }}
</style>
</head>
<body>
<h1>kbGen Dashboard</h1>
<div class="sub">Project: {project_name} &nbsp;|&nbsp; Sessions: {len(sessions)} &nbsp;|&nbsp; Generated: {__import__('datetime').datetime.now().strftime('%Y-%m-%d %H:%M')}</div>

<div class="cards">
  <div class="card"><div class="card-label">Quality</div><div class="card-value">{grade} ({score_str})</div></div>
  <div class="card"><div class="card-label">Avg Saving</div><div class="card-value">{saving_str}</div></div>
  <div class="card"><div class="card-label">Sessions</div><div class="card-value">{len(sessions)}</div></div>
  <div class="card"><div class="card-label">Snap sessions</div><div class="card-value">{len(snap_sessions)}</div></div>
</div>

<div class="section">
  <h2>Token Trend (last 20 sessions per group)</h2>
  {chart_svg}
  <div class="legend"><span class="snap">━━</span> with snapshot &nbsp; <span class="nosnap">╌╌</span> without snapshot (higher = more tokens)</div>
</div>

<div class="section">
  <h2>Snapshot Quality</h2>
  <div class="q-row"><span class="q-label">Score</span><span class="q-val">{score_str}</span></div>
  <div class="q-row"><span class="q-label">Coverage</span><span class="q-val">{cov_str}%</span>&nbsp;{_cov_bar(cov)}</div>
  <div class="q-row"><span class="q-label">Freshness</span><span class="q-val">{fresh_str}%</span>&nbsp;{_cov_bar(fresh)}</div>
  {"<div style='color:#f38ba8;font-size:0.85em;margin-top:8px'>⚠ " + str(stale) + " files changed since last scan — run <code>kbgen update</code></div>" if stale and stale > 0 else ""}
</div>

<div class="section">
  <h2>Session History (last 50)</h2>
  <table id="tbl">
    <thead><tr>
      <th onclick="sort(0)">Time</th>
      <th onclick="sort(1)">Snap</th>
      <th onclick="sort(2)">Total Input</th>
      <th onclick="sort(3)">Output</th>
      <th onclick="sort(4)">Cache Read</th>
      <th onclick="sort(5)">Duration</th>
      <th onclick="sort(6)">Quality</th>
    </tr></thead>
    <tbody>{rows_html}</tbody>
  </table>
</div>

<script>
const data = {session_data};
let sortDir = {{}};
function sort(col) {{
  const tb = document.querySelector('#tbl tbody');
  const rows = Array.from(tb.rows);
  sortDir[col] = !(sortDir[col]);
  rows.sort((a, b) => {{
    const av = a.cells[col].textContent.trim();
    const bv = b.cells[col].textContent.trim();
    const an = parseFloat(av.replace(/,/g, ''));
    const bn = parseFloat(bv.replace(/,/g, ''));
    const cmp = isNaN(an) ? av.localeCompare(bv) : an - bn;
    return sortDir[col] ? cmp : -cmp;
  }});
  rows.forEach(r => tb.appendChild(r));
}}
</script>
</body>
</html>"""
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
python -m pytest tests/test_report.py -v
```

Expected: all 7 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add kbgen/report.py tests/test_report.py
git commit -m "feat: add report.py HTML dashboard generator"
```

---

## Task 5: Add `show_dashboard()` to `gain.py`

**Files:**
- Modify: `kbgen/gain.py`

- [ ] **Step 1: Add imports at top of `gain.py`**

After the existing imports at top of `kbgen/gain.py`, add:

```python
from pathlib import Path
```

(Note: `Path` is not currently imported in gain.py — add it.)

- [ ] **Step 2: Add `_sparkline` helper and `show_dashboard` function at end of `gain.py`**

Append to `kbgen/gain.py`:

```python
_SPARK_BARS = "▁▂▃▄▅▆▇█"


def _sparkline(values: list[float]) -> str:
    if not values:
        return ""
    lo, hi = min(values), max(values)
    rng = hi - lo if hi != lo else 1
    return "".join(
        _SPARK_BARS[int((v - lo) / rng * (len(_SPARK_BARS) - 1))]
        for v in values
    )


def show_dashboard(
    root: Path,
    n_recent: int = 10,
    no_html: bool = False,
    auto_open: bool = False,
    output_path: Path | None = None,
) -> None:
    from kbgen.quality import compute_quality, format_quality_terminal
    from kbgen.report import generate_html

    sessions = _load_sessions()
    quality = compute_quality(root)

    snap_sessions = [s for s in sessions if s.get("snapshot")]
    no_snap_sessions = [s for s in sessions if not s.get("snapshot")]

    project_name = root.name

    width = 56
    border = "═" * width

    print(f"╔{border}╗")
    print(f"║{'kbGen Dashboard':^{width}}║")
    print(f"║{f'Project: {project_name}':^{width}}║")
    print(f"╚{border}╝")
    print()

    # Token savings
    if snap_sessions and no_snap_sessions:
        avg_snap = sum(_total_input(s) for s in snap_sessions) / len(snap_sessions)
        avg_no_snap = sum(_total_input(s) for s in no_snap_sessions) / len(no_snap_sessions)
        saving_pct = (1 - avg_snap / avg_no_snap) * 100 if avg_no_snap > 0 else 0.0
        direction = "▲" if saving_pct >= 0 else "▼"
        print("TOKEN SAVINGS")
        print(f"  With snapshot:    avg {_fmt_num(int(avg_snap)):>9} input/session  ({len(snap_sessions)} sessions)")
        print(f"  Without snapshot: avg {_fmt_num(int(avg_no_snap)):>9} input/session  ({len(no_snap_sessions)} sessions)")
        print(f"  Estimated saving: {direction} {abs(saving_pct):.1f}%")
        print()
    elif snap_sessions:
        print(f"TOKEN SAVINGS: {len(snap_sessions)} snap sessions, no baseline yet.")
        print()
    else:
        print("TOKEN SAVINGS: no sessions recorded.")
        print()

    # Trend sparkline
    recent_snap = [_total_input(s) for s in snap_sessions[-n_recent:]]
    snap_used = ["✓" if s.get("snapshot") else "✗" for s in sessions[-n_recent:]]
    if recent_snap:
        print(f"TREND (last {len(recent_snap)} snap sessions, input tokens lower=better)")
        print(f"  {_sparkline(recent_snap)}")
        if len(sessions) >= 2:
            recent_all = sessions[-n_recent:]
            print(f"  {''.join(snap_used)}  ← snapshot used (last {len(recent_all)})")
        print()

    # Quality
    print("QUALITY")
    print(format_quality_terminal(quality, indent="  "))
    print()

    # HTML
    if not no_html:
        out = output_path or (root / ".ai" / "dashboard.html")
        out.parent.mkdir(parents=True, exist_ok=True)
        html = generate_html(sessions, quality if quality.get("available") else None, project_name)
        out.write_text(html, encoding="utf-8")
        print(f"HTML report: {out}")
        if auto_open:
            import webbrowser
            webbrowser.open(out.as_uri())
        else:
            answer = input("Open in browser? [y/N] ").strip().lower()
            if answer == "y":
                import webbrowser
                webbrowser.open(out.as_uri())
```

- [ ] **Step 3: Verify no syntax errors**

```bash
python -c "from kbgen.gain import show_dashboard; print('ok')"
```

Expected: `ok`

- [ ] **Step 4: Commit**

```bash
git add kbgen/gain.py
git commit -m "feat: add show_dashboard() with sparkline trend and quality panel"
```

---

## Task 6: Add `dashboard` subcommand to `cli.py`

**Files:**
- Modify: `kbgen/cli.py`

- [ ] **Step 1: Add import at top of `cli.py`**

After the existing imports in `kbgen/cli.py` (after `from kbgen.gain import show_gain`), add:

```python
from kbgen.gain import show_dashboard
```

(Note: `show_gain` is already imported — change that import line to import both:)

```python
from kbgen.gain import show_gain, show_dashboard
```

- [ ] **Step 2: Add `dashboard` parser in `build_parser()`**

After the `gain_parser` block (after the `--last` argument for gain), add:

```python
dashboard_parser = sub.add_parser("dashboard", help="Show token savings dashboard (terminal + HTML)")
dashboard_parser.add_argument(
    "--last",
    type=int,
    default=10,
    metavar="N",
    help="Trend sparkline sessions (default: 10)",
)
dashboard_parser.add_argument(
    "--no-html",
    action="store_true",
    help="Skip HTML report generation",
)
dashboard_parser.add_argument(
    "--open",
    action="store_true",
    dest="auto_open",
    help="Auto-open HTML report in browser",
)
dashboard_parser.add_argument(
    "--output",
    default=None,
    help="HTML output path (default: .ai/dashboard.html)",
)
```

- [ ] **Step 3: Add `dashboard` handler in `main()`**

After the `if args.command == "gain":` block in `main()`, add:

```python
if args.command == "dashboard":
    output_path = Path(args.output).resolve() if args.output else None
    show_dashboard(
        root=root,
        n_recent=args.last,
        no_html=args.no_html,
        auto_open=args.auto_open,
        output_path=output_path,
    )
    return 0
```

- [ ] **Step 4: Verify CLI parses correctly**

```bash
python -m kbgen dashboard --help
```

Expected output shows `--last`, `--no-html`, `--open`, `--output` options.

- [ ] **Step 5: Commit**

```bash
git add kbgen/cli.py
git commit -m "feat: add dashboard subcommand to CLI"
```

---

## Task 7: Run full test suite and smoke test

**Files:** none modified

- [ ] **Step 1: Run all tests**

```bash
python -m pytest tests/ -v
```

Expected: all tests PASS (test_quality.py × 7, test_report.py × 7).

- [ ] **Step 2: Smoke test dashboard with no sessions**

```bash
python -m kbgen dashboard --no-html
```

Expected: prints dashboard with "no sessions recorded" message, no crash.

- [ ] **Step 3: Smoke test quality with real project snapshot (if .ai/snapshot.kb exists)**

```bash
python -c "
from pathlib import Path
from kbgen.quality import compute_quality, format_quality_terminal
q = compute_quality(Path('.'))
print(format_quality_terminal(q))
"
```

Expected: prints quality block or "no snapshot found".

- [ ] **Step 4: Verify gain command still works unchanged**

```bash
python -m kbgen gain --last 5
```

Expected: existing gain output unchanged.

- [ ] **Step 5: Final commit**

```bash
git add -A
git commit -m "test: verify dashboard integration end-to-end"
```

---

## Self-Review Notes

**Spec coverage check:**
- ✓ Real-time per-session feedback → Task 3 enhances `format_summary` with savings % and quality grade
- ✓ Trend analysis → Task 5 `show_dashboard` sparkline + A/B table
- ✓ Snapshot quality score → Task 2 `quality.py` coverage+staleness+grade
- ✓ A/B comparison → Task 5 `show_dashboard` TOKEN SAVINGS section
- ✓ Terminal output → Tasks 3, 5
- ✓ HTML output → Tasks 4, 5
- ✓ `kbgen dashboard` CLI → Task 6
- ✓ Old sessions.jsonl backward compat → `quality.py` returns `available=False` gracefully, gain.py uses `.get()` with defaults
- ✓ `--no-html`, `--open`, `--output`, `--last` flags → Task 6

**Type consistency:**
- `compute_quality()` returns `dict[str, Any]` — used consistently in Tasks 3, 5
- `format_quality_terminal(q, indent)` — called in Task 5 with `quality` dict from Task 2
- `show_dashboard(root, n_recent, no_html, auto_open, output_path)` — matches cli.py call in Task 6
- `generate_html(sessions, quality, project_name)` — matches test_report.py and gain.py call
