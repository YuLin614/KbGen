from __future__ import annotations

import datetime
import json
from typing import Any

_BARS = "▁▂▃▄▅▆▇█"


def _sparkline(values: list[float]) -> str:
    if not values:
        return ""
    lo, hi = min(values), max(values)
    rng = hi - lo if hi != lo else 1
    return "".join(
        _BARS[int((v - lo) / rng * (len(_BARS) - 1))]
        for v in values
    )


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


def _cov_bar(pct: float | None, width: int = 120) -> str:
    if pct is None:
        return ""
    filled = int(pct / 100 * width)
    return (
        f'<div style="background:#1e1e2e;border-radius:4px;height:12px;width:{width}px">'
        f'<div style="background:#4ade80;height:12px;width:{filled}px;border-radius:4px"></div>'
        f'</div>'
    )


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
    staleness = q.get("staleness_pct")
    fresh = (100.0 - staleness) if staleness is not None else None
    fresh_str = f"{fresh:.1f}" if fresh is not None else "n/a"
    saving_str = f"{saving_pct:.1f}%" if saving_pct is not None else "n/a"

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

    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    stale_warning = (
        f"<div style='color:#f38ba8;font-size:0.85em;margin-top:8px'>"
        f"&#9888; {stale} files changed since last scan — run <code>kbgen update</code></div>"
        if stale and stale > 0 else ""
    )

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
<div class="sub">Project: {project_name} &nbsp;|&nbsp; Sessions: {len(sessions)} &nbsp;|&nbsp; Generated: {now}</div>

<div class="cards">
  <div class="card"><div class="card-label">Quality</div><div class="card-value">{grade} ({score_str})</div></div>
  <div class="card"><div class="card-label">Avg Saving</div><div class="card-value">{saving_str}</div></div>
  <div class="card"><div class="card-label">Sessions</div><div class="card-value">{len(sessions)}</div></div>
  <div class="card"><div class="card-label">Snap sessions</div><div class="card-value">{len(snap_sessions)}</div></div>
</div>

<div class="section">
  <h2>Token Trend (last 20 sessions per group)</h2>
  {chart_svg}
  <div class="legend"><span class="snap">&#9473;&#9473;</span> with snapshot &nbsp; <span class="nosnap">&#9484;&#9484;</span> without snapshot (higher = more tokens)</div>
</div>

<div class="section">
  <h2>Snapshot Quality</h2>
  <div class="q-row"><span class="q-label">Score</span><span class="q-val">{score_str}</span></div>
  <div class="q-row"><span class="q-label">Coverage</span><span class="q-val">{cov_str}%</span>&nbsp;{_cov_bar(cov)}</div>
  <div class="q-row"><span class="q-label">Freshness</span><span class="q-val">{fresh_str}%</span>&nbsp;{_cov_bar(fresh)}</div>
  {stale_warning}
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
