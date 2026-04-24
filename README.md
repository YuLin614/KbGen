# kbgen

`kbgen` is a CLI that generates an offline semantic snapshot for codebase exploration.

## Commands

- `kbgen init` - create `.ai/` and default artifacts
- `kbgen scan` - full cold-start generation
- `kbgen update` - incremental update based on git changes
- `kbgen benchmark` - compare baseline vs snapshot run logs

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

Install on a teammate machine with pipx (recommended):

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
