# kbgen

`kbgen` is a CLI that generates an offline, token-optimized semantic snapshot for codebase exploration.

## Commands

- `kbgen init` - create `.ai/` and default artifacts
- `kbgen scan` - full cold-start generation
- `kbgen update` - incremental update based on git changes
- `kbgen benchmark` - compare baseline vs snapshot run logs

## Artifacts

- `.ai/snapshot.kb`
- `.ai/schema.kb`
- `.ai/meta.json`

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

Example JSONL line:

```json
{"task_id":"add-endpoint-1","input_tokens":3200,"success":true,"loops":2}
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

Install from wheel on a teammate machine:

```powershell
python -m pip install path/to/kbgen-0.1.0-py3-none-any.whl
kbgen --help
```

You can also use helper scripts in `scripts/`:

- `scripts/build-release.ps1` builds package files and creates `release/kbgen-<version>-windows.zip`
- `scripts/install-kbgen.ps1` installs kbgen from a wheel file and validates the command
