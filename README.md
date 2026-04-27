# kbgen

`kbgen` is a CLI that generates an offline semantic snapshot for codebase exploration.

## Installation

```powershell
winget install astral-sh.uv   # skip if uv already installed
uv tool install git+https://github.com/YuLin614/KbGen.git
```

Installs both `kbgen` and `kbclaude` executables. If `uv` is not available via winget, get it from [docs.astral.sh/uv](https://docs.astral.sh/uv).

## CLAUDE.md Setup

After installing, add the following to your project's `CLAUDE.md` so Claude knows to use the snapshot:

```markdown
## Codebase navigation

At the start of a new feature development session, read `.ai/snapshot.kb` (schema: `.ai/schema.kb`) once before writing any code. Use it for navigation only — finding file locations, not understanding logic. Key fields: `a` (symbol→file:line), `p` (file inventory per module), `ri` (route→file mapping), `hf`/`hr` (task entry points by type), `fd` (file dependency edges). If a path from snapshot is not found on disk, fall back to `Glob` — snapshot may be stale mid-session.
```

Then generate the snapshot once:

```bash
kbgen init
kbgen scan
```

## Commands

- `kbgen init` - create `.ai/` and default artifacts
- `kbgen scan` - full cold-start generation
- `kbgen update` - incremental update based on git changes
- `kbgen benchmark` - compare baseline vs snapshot run logs
- `kbclaude` - run Claude CLI with Anthropic token usage tracking (also available as `kbgen claude`)

Tune file-path granularity in snapshot:

```bash
kbgen scan --path-limit 5
```

`--path-limit` controls how many key file paths (`p`) are kept per module (default: 0, unlimited).

Token budget eviction is disabled to preserve full snapshot detail during cold start.

Snapshot size now uses soft overflow control: when payload grows too large, verbose fields like `p` and `fd` are compacted automatically to reduce agent read-token cost while preserving navigation signals.

## Artifacts

- `.ai/snapshot.kb`
- `.ai/schema.kb`
- `.ai/meta.json`

Notes:

- Build/output folders like `.next`, `coverage`, `lcov-report`, `dist`, `build`, `out`, and `tmp` are ignored during scan.
- `fd` (file dependency digest) now scales with repository size instead of being fixed to a hard 24-entry cap.
- `cy` lists detected mutual module dependency cycles (e.g., `app<->components`) to highlight architecture hot spots.
- `hr` is a machine-readable read plan aligned with `hf` (`s`=step, `t`=target, `r`=reason code) for agent-first navigation.

## Token Usage Tracking

`kbclaude` is a drop-in wrapper for the Claude CLI that intercepts every Anthropic API call and prints a token usage summary when the session ends.

```bash
kbclaude                        # launch Claude CLI with tracking
kbclaude --model sonnet-4-5     # flags forwarded verbatim
kbgen claude                    # same, via kbgen subcommand
```

Requires [Claude Code CLI](https://claude.ai/code) to be installed and available on `PATH`.

How it works: a local HTTP proxy starts on `localhost:<random port>`. The environment variable `ANTHROPIC_BASE_URL` is set to point Claude CLI at the proxy. The proxy intercepts all API calls, parses token usage from both streaming (SSE) and non-streaming responses, accumulates totals, then shuts down when Claude CLI exits.

On exit:

```
--- kbclaude session summary ---
  Requests          : 8
  Input tokens      : 56  (uncached)
  Cache write tokens: 71,570
  Cache read tokens : 455,920
  Total input       : 527,546  (uncached + cache_write + cache_read)
  Output tokens     : 1,618
  Duration          : 866.1s
--------------------------------
```

Field notes:

- **Input tokens** — tokens sent that were not served from cache
- **Cache write tokens** — tokens written to prompt cache this session (billed at ~1.25x)
- **Cache read tokens** — tokens served from existing cache (billed at ~0.1x, much cheaper)
- **Total input** — full context processed on the input side
- **Output tokens** — tokens Claude generated; this is the most expensive category (~5x input rate) and the best indicator of how much work Claude actually did

Output tokens are the primary metric for evaluating whether `kbgen scan` snapshot is helping: fewer output tokens means Claude spent less time exploring the codebase.

## Benchmark

Use A/B experiment logs to evaluate whether snapshot usage reduces token cost without hurting quality.

```bash
kbgen benchmark --baseline baseline.jsonl --snapshot with_snapshot.jsonl
```

Generate markdown report too:

```bash
kbgen benchmark --baseline baseline.jsonl --snapshot with_snapshot.jsonl --markdown
```

Input format supports JSON array or JSONL. Each record should include:

- `task_id` (or `task`/`id`)
- `input_tokens` (or `tokens`)
- `success` (boolean-like)
- `loops` (or `iterations`/`retries`)

Optional fields for first-read hit analysis:

- `first_read_path` (or `first_reads`): first file(s) the agent read
- `target_paths` (or `expected_paths`/`gold_paths`): expected correct file target(s)

Example JSONL line:

```json
{"task_id":"add-endpoint-1","input_tokens":3200,"success":true,"loops":2}
```

Example with first-read metrics:

```json
{"task_id":"bug-42","task_type":"bugfix","input_tokens":2800,"success":true,"loops":1,"first_read_path":"src/api/router.ts","target_paths":["src/api/router.ts","src/services/user.ts"]}
```

Optional grouping key:

- `task_type` (or `type`/`category`/`kind`), e.g. `feature`, `bugfix`, `refactor`

By default, report is saved to `.ai/benchmark-report.json` with pass/fail gate:

- median token savings >= 20%
- success rate drop <= 5%
- mean loop count does not increase

When `--markdown` is enabled, a readable report is also saved to `.ai/benchmark-report.md`.

## Team Distribution

Build distributable artifacts:

```powershell
python -m pip install build
python -m build
```

This creates:

- `dist/kbgen-<version>-py3-none-any.whl`
- `dist/kbgen-<version>.tar.gz`

Alternatively, install from a wheel file:

```powershell
uv tool install path/to/kbgen-0.1.0-py3-none-any.whl
```

Alternatively with pipx:

```powershell
py -m pip install --user pipx
py -m pipx ensurepath
py -m pipx install path/to/kbgen-0.1.0-py3-none-any.whl
kbgen --help
```

Recommended on Windows (auto-installs pipx and prints PATH fix commands if needed):

```powershell
./scripts/install-kbgen.ps1 -WheelPath dist/kbgen-0.1.0-py3-none-any.whl
```

One command for build + local install + validation:

```powershell
./scripts/release-and-install.ps1 -BuildPythonExe .venv/Scripts/python.exe
```

If `kbgen` is not recognized after install, run either:

- open a new PowerShell window, or
- temporary PATH update in current shell: `$env:Path += ';$HOME\\.local\\bin'`

PowerShell example:

```powershell
$env:Path += ';$HOME\.local\bin'
kbgen --help
```

You can also use helper scripts in `scripts/`:

- `scripts/build-release.ps1` builds package files and creates `release/kbgen-<version>-windows.zip`
- `scripts/install-kbgen.ps1` installs kbgen globally with pipx and validates the command
- `scripts/release-and-install.ps1` runs build + local install + command validation in one step
