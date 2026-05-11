# kbGen Visibility Dashboard — Design Spec
Date: 2026-05-11

## Problem

kbGen already tracks token usage via `claude_wrapper.py` and `gain.py`, but visibility is weak:
- No real-time per-session feedback on savings
- No snapshot quality signal (is the snapshot still accurate?)
- No A/B trend analysis across sessions
- No HTML output for deep analysis

## Goal

Make kbGen's value measurable and visible at two levels:
1. **Live** — every `kbclaude` session ends with a meaningful summary
2. **Deep** — `kbgen dashboard` produces terminal overview + HTML report

## Architecture

Three layers, zero new dependencies:

```
Trigger Layer
  kbclaude exit     → enhanced terminal session summary
  kbgen dashboard   → terminal overview + HTML report

Compute Layer (new modules)
  quality.py        → coverage % + staleness % + quality score
  report.py         → pure HTML/CSS/SVG generator
  gain.py (enhanced)→ trend analysis, A/B grouping

Data Layer (existing, extended)
  ~/.kbgen/sessions.jsonl  → add quality fields per session
  .ai/meta.json            → snapshot age (already exists)
```

## Data Layer Changes

Extend `sessions.jsonl` records with new fields. Old records missing these fields fall back to `null` gracefully.

```json
{
  "ts": "2026-05-11T10:30:00Z",
  "snapshot": true,
  "input_tokens": 2847,
  "output_tokens": 634,
  "cache_write_tokens": 0,
  "cache_read_tokens": 41200,
  "duration_s": 94.2,
  "snapshot_tokens": 11400,
  "snapshot_coverage_pct": 82.3,
  "snapshot_staleness_pct": 5.1,
  "snapshot_quality_score": 76
}
```

Quality fields are computed once per session start (before `kbclaude` launches Claude) and written alongside token fields at session end.

## Quality Scoring (`quality.py`)

Two static metrics, no AI behavior tracking required.

### Coverage
```
coverage_pct = files referenced in snapshot / total source files × 100
```
- Source: snapshot `p` (key paths) fields across all modules
- Total: `parsing.collect_source_files()` count
- Target: >70%

### Staleness
```
staleness_pct = files changed since snapshot generated / files in snapshot × 100
```
- Snapshot age: `.ai/meta.json` → `generated_at`
- Changed files: `git log --since=<generated_at> --name-only --format=` filtered to source extensions
- Fallback (no git): compare file mtimes against `generated_at`

### Composite Score
```
quality_score = coverage_pct × 0.6 + (100 − staleness_pct) × 0.4
```

Grade: A(≥85) / B(70–84) / C(50–69) / D(<50)

### Terminal Display
```
Snapshot Quality: B (76/100)
  Coverage:   82% ████████░░  (164/200 files)
  Freshness:  94% █████████░  (12 files changed since scan)
```

## `kbgen dashboard` Command

### Terminal Output
```
═══════════════════ kbGen Dashboard ════════════════════
Project: my-app    Snapshot: 11,400 tokens   Quality: B(76)

TOKEN SAVINGS (last 30 days)
  With snapshot:    avg 3,200 input/session   (28 sessions)
  Without snapshot: avg 4,600 input/session   ( 3 sessions)
  Estimated saving: ▲ 30.4%   ~$0.84 saved

TREND (last 10 sessions)
  ▂▃▄▄▅▃▄▅▅▄  input tokens (lower = better)
  ✓ ✓ ✓ ✓ ✓ ✓ ✓ ✓ ✓ ✓  snapshot used

QUALITY
  Coverage:  82%  ████████░░
  Freshness: 94%  █████████░
  → Run `kbgen update` (12 files changed since last scan)

HTML report: .ai/dashboard.html  (open? [y/N])
════════════════════════════════════════════════════════
```

### CLI Arguments
```
kbgen dashboard [--last N]        # limit to last N sessions (default: all)
                [--no-html]       # skip HTML generation
                [--open]          # auto-open HTML in browser
                [--output PATH]   # HTML output path (default: .ai/dashboard.html)
```

## Enhanced `kbclaude` Session Summary

Appended to existing output at session end:

```
── Session Summary ──────────────────────────────────────
  Input:  2,847 tokens  Cache hit: 41,200  Output: 634
  Snapshot used: yes  (11,400 tokens injected)
  Est. saving vs baseline: ▲ ~28%  ($0.031)
  Quality: B(76) — 12 files stale, consider `kbgen update`
─────────────────────────────────────────────────────────
```

Quality and staleness warning computed fresh each session. If no without-snapshot sessions exist for comparison, saving estimate shows `n/a (no baseline sessions)`.

## HTML Report (`report.py`)

Single self-contained `.html` file. Zero external dependencies — pure inline HTML/CSS/SVG/JS.

### Page Layout
```
┌─────────────────────────────────────────────┐
│  kbGen Dashboard  •  project  •  date        │
├──────────┬──────────┬──────────┬────────────┤
│ Quality  │ Avg Save │ Sessions │ Est. $saved │
├──────────┴──────────┴──────────┴────────────┤
│ SVG line chart: input tokens per session     │
│ (with-snapshot line vs without-snapshot line)│
├─────────────────────────────────────────────┤
│ Quality panel: Coverage bar  Freshness bar   │
│ Coverage gap: files not referenced in snap   │
├─────────────────────────────────────────────┤
│ Session history table (JS sortable)          │
│ ts | input | cache_hit | snapshot | saving%  │
└─────────────────────────────────────────────┘
```

### Technical Choices
- SVG line chart: Python-generated coordinate points, no JS chart library
- Table sorting: vanilla JS, <50 lines, no framework
- Styling: inline CSS, dark theme, monochrome
- Data: JSON embedded in `<script>` tag, no server needed
- Open: `webbrowser.open()` triggered by `--open` flag

## Implementation Scope

### New Files
- `kbgen/quality.py` — coverage + staleness + score computation
- `kbgen/report.py` — HTML generation

### Modified Files
- `kbgen/gain.py` — add trend analysis, A/B grouping, quality display
- `kbgen/claude_wrapper.py` — compute quality at session start, write new fields, enhance summary
- `kbgen/cli.py` — add `dashboard` subcommand
- `kbgen/constants.py` — add quality grade thresholds, session field names

### Out of Scope
- Real-time streaming token counter (UI complexity not justified)
- AI behavior tracking / hint hit rate (requires prompt instrumentation)
- Remote/cloud dashboard (local only)
- Cost estimation per-model pricing table (use existing logic in gain.py)

## Success Criteria

1. `kbclaude` session end shows saving % and quality grade
2. `kbgen dashboard` prints terminal overview without errors on projects with ≥1 session
3. `kbgen dashboard` generates valid HTML viewable in browser
4. Quality score computed correctly: coverage matches actual file counts, staleness matches `git diff` output
5. Old `sessions.jsonl` records (missing new fields) handled gracefully — no crash, fields shown as `n/a`
