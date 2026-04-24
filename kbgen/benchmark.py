from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from pathlib import Path
from statistics import mean, median
from typing import Any


@dataclass
class RunRecord:
    task_id: str
    task_type: str
    input_tokens: float
    success: bool
    loops: float
    first_reads: list[str]
    target_paths: list[str]


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y", "ok", "pass"}
    return False


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _pick(data: dict[str, Any], keys: list[str], default: Any = None) -> Any:
    for key in keys:
        if key in data:
            return data[key]
    return default


def _norm_path(value: str) -> str:
    return value.strip().replace("\\", "/").lower()


def _as_path_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        parts = [p for p in re.split(r"[|,;]", value) if p.strip()]
        return [_norm_path(p) for p in parts]
    if isinstance(value, list):
        out: list[str] = []
        for item in value:
            if isinstance(item, str) and item.strip():
                out.append(_norm_path(item))
        return out
    return []


def _normalize_record(item: dict[str, Any], index: int) -> RunRecord:
    task_id = str(_pick(item, ["task_id", "task", "id"], f"task-{index + 1}"))
    task_type = str(_pick(item, ["task_type", "type", "category", "kind"], "unknown"))
    input_tokens = _as_float(_pick(item, ["input_tokens", "tokens", "token_input", "total_input_tokens"], 0.0), 0.0)
    success = _as_bool(_pick(item, ["success", "passed", "ok"], False))
    loops = _as_float(_pick(item, ["loops", "iterations", "retries"], 0.0), 0.0)
    first_reads = _as_path_list(_pick(item, ["first_reads", "first_read", "first_read_path", "first_read_file"], []))
    target_paths = _as_path_list(_pick(item, ["target_paths", "expected_paths", "gold_paths", "target_files"], []))
    return RunRecord(
        task_id=task_id,
        task_type=task_type,
        input_tokens=input_tokens,
        success=success,
        loops=loops,
        first_reads=first_reads,
        target_paths=target_paths,
    )


def load_run_records(path: Path) -> list[RunRecord]:
    text = path.read_text(encoding="utf-8-sig").strip()
    if not text:
        return []

    rows: list[dict[str, Any]] = []
    if text.startswith("["):
        parsed = json.loads(text)
        if not isinstance(parsed, list):
            raise ValueError(f"Expected JSON array in {path}")
        rows = [r for r in parsed if isinstance(r, dict)]
    else:
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            if isinstance(row, dict):
                rows.append(row)

    return [_normalize_record(row, i) for i, row in enumerate(rows)]


def _percentile(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    if p <= 0:
        return min(values)
    if p >= 100:
        return max(values)
    ordered = sorted(values)
    k = (len(ordered) - 1) * (p / 100)
    lower = math.floor(k)
    upper = math.ceil(k)
    if lower == upper:
        return ordered[lower]
    weight = k - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight


def _rate(true_count: int, total: int) -> float:
    if total <= 0:
        return 0.0
    return true_count / total


def _first_read_hit(record: RunRecord) -> bool:
    if not record.first_reads or not record.target_paths:
        return False
    for read in record.first_reads:
        for target in record.target_paths:
            if read == target or read.endswith(target) or target.endswith(read):
                return True
    return False


def summarize(records: list[RunRecord]) -> dict[str, Any]:
    tokens = [r.input_tokens for r in records]
    loops = [r.loops for r in records]
    success_count = sum(1 for r in records if r.success)
    hit_count = sum(1 for r in records if _first_read_hit(r))
    eligible = sum(1 for r in records if r.first_reads and r.target_paths)

    return {
        "tasks": len(records),
        "tokens": {
            "mean": mean(tokens) if tokens else 0.0,
            "median": median(tokens) if tokens else 0.0,
            "p90": _percentile(tokens, 90),
            "total": sum(tokens),
        },
        "success_rate": _rate(success_count, len(records)),
        "loops": {
            "mean": mean(loops) if loops else 0.0,
            "median": median(loops) if loops else 0.0,
            "total": sum(loops),
        },
        "first_read_hit_rate": _rate(hit_count, eligible),
        "first_read_eligible_tasks": eligible,
    }


def summarize_by_type(records: list[RunRecord]) -> dict[str, dict[str, Any]]:
    grouped: dict[str, list[RunRecord]] = {}
    for r in records:
        grouped.setdefault(r.task_type, []).append(r)
    return {k: summarize(v) for k, v in sorted(grouped.items())}


def _type_deltas(
    baseline: list[RunRecord],
    with_snapshot: list[RunRecord],
) -> dict[str, dict[str, float]]:
    base_group = summarize_by_type(baseline)
    snap_group = summarize_by_type(with_snapshot)

    out: dict[str, dict[str, float]] = {}
    for task_type in sorted(set(base_group.keys()) & set(snap_group.keys())):
        base = base_group[task_type]
        snap = snap_group[task_type]
        base_med = base["tokens"]["median"]
        snap_med = snap["tokens"]["median"]
        median_savings = ((base_med - snap_med) / base_med) if base_med > 0 else 0.0

        base_total = base["tokens"]["total"]
        snap_total = snap["tokens"]["total"]
        total_savings = ((base_total - snap_total) / base_total) if base_total > 0 else 0.0

        out[task_type] = {
            "median_token_savings_rate": median_savings,
            "total_token_savings_rate": total_savings,
            "success_rate_drop": base["success_rate"] - snap["success_rate"],
            "mean_loop_delta": snap["loops"]["mean"] - base["loops"]["mean"],
            "first_read_hit_rate_delta": snap.get("first_read_hit_rate", 0.0) - base.get("first_read_hit_rate", 0.0),
        }
    return out


def _pair_by_task(
    baseline: list[RunRecord],
    with_snapshot: list[RunRecord],
) -> tuple[list[tuple[RunRecord, RunRecord]], list[str]]:
    base_map = {r.task_id: r for r in baseline}
    snap_map = {r.task_id: r for r in with_snapshot}

    shared = sorted(set(base_map.keys()) & set(snap_map.keys()))
    missing = sorted((set(base_map.keys()) ^ set(snap_map.keys())))
    return ([(base_map[t], snap_map[t]) for t in shared], missing)


def compare_runs(
    baseline: list[RunRecord],
    with_snapshot: list[RunRecord],
    min_savings: float,
    max_success_drop: float,
) -> dict[str, Any]:
    pairs, missing = _pair_by_task(baseline, with_snapshot)
    if not pairs:
        raise ValueError("No overlapping task_id between baseline and snapshot runs")

    base_summary = summarize([p[0] for p in pairs])
    snap_summary = summarize([p[1] for p in pairs])

    base_tokens = base_summary["tokens"]["median"]
    snap_tokens = snap_summary["tokens"]["median"]
    median_savings = ((base_tokens - snap_tokens) / base_tokens) if base_tokens > 0 else 0.0

    base_total = base_summary["tokens"]["total"]
    snap_total = snap_summary["tokens"]["total"]
    total_savings = ((base_total - snap_total) / base_total) if base_total > 0 else 0.0

    success_drop = base_summary["success_rate"] - snap_summary["success_rate"]
    loop_delta = snap_summary["loops"]["mean"] - base_summary["loops"]["mean"]
    first_read_hit_delta = snap_summary.get("first_read_hit_rate", 0.0) - base_summary.get("first_read_hit_rate", 0.0)

    pass_gate = (
        median_savings >= min_savings
        and success_drop <= max_success_drop
        and loop_delta <= 0
    )

    return {
        "paired_tasks": len(pairs),
        "missing_task_ids": missing,
        "baseline": base_summary,
        "with_snapshot": snap_summary,
        "by_task_type": {
            "baseline": summarize_by_type([p[0] for p in pairs]),
            "with_snapshot": summarize_by_type([p[1] for p in pairs]),
            "delta": _type_deltas([p[0] for p in pairs], [p[1] for p in pairs]),
        },
        "delta": {
            "median_token_savings_rate": median_savings,
            "total_token_savings_rate": total_savings,
            "success_rate_drop": success_drop,
            "mean_loop_delta": loop_delta,
            "first_read_hit_rate_delta": first_read_hit_delta,
        },
        "thresholds": {
            "min_savings": min_savings,
            "max_success_drop": max_success_drop,
            "require_non_increasing_loops": True,
        },
        "pass": pass_gate,
    }


def benchmark(
    baseline_file: Path,
    snapshot_file: Path,
    output_file: Path | None,
    min_savings: float,
    max_success_drop: float,
) -> dict[str, Any]:
    baseline_runs = load_run_records(baseline_file)
    snapshot_runs = load_run_records(snapshot_file)

    report = compare_runs(
        baseline=baseline_runs,
        with_snapshot=snapshot_runs,
        min_savings=min_savings,
        max_success_drop=max_success_drop,
    )

    if output_file is not None:
        output_file.parent.mkdir(parents=True, exist_ok=True)
        output_file.write_text(json.dumps(report, indent=2, ensure_ascii=True) + "\n", encoding="utf-8")

    return report


def format_markdown_report(report: dict[str, Any]) -> str:
    delta = report.get("delta", {})
    baseline = report.get("baseline", {})
    snapshot = report.get("with_snapshot", {})
    thresholds = report.get("thresholds", {})
    by_type = report.get("by_task_type", {}).get("delta", {})

    lines: list[str] = []
    lines.append("# kbgen Benchmark Report")
    lines.append("")
    lines.append(f"- Gate result: {'PASS' if report.get('pass') else 'FAIL'}")
    lines.append(f"- Paired tasks: {report.get('paired_tasks', 0)}")
    lines.append(f"- Missing task IDs: {', '.join(report.get('missing_task_ids', [])) or 'none'}")
    lines.append("")
    lines.append("## Overall Metrics")
    lines.append("")
    lines.append(f"- Median token savings: {delta.get('median_token_savings_rate', 0.0):.2%}")
    lines.append(f"- Total token savings: {delta.get('total_token_savings_rate', 0.0):.2%}")
    lines.append(f"- Success rate drop: {delta.get('success_rate_drop', 0.0):.2%}")
    lines.append(f"- Mean loop delta: {delta.get('mean_loop_delta', 0.0):.3f}")
    lines.append(f"- First-read hit rate delta: {delta.get('first_read_hit_rate_delta', 0.0):.2%}")
    lines.append("")
    lines.append("## Baseline")
    lines.append("")
    lines.append(
        f"- Tokens mean/median/p90/total: {baseline.get('tokens', {}).get('mean', 0.0):.2f} / "
        f"{baseline.get('tokens', {}).get('median', 0.0):.2f} / "
        f"{baseline.get('tokens', {}).get('p90', 0.0):.2f} / "
        f"{baseline.get('tokens', {}).get('total', 0.0):.2f}"
    )
    lines.append(f"- Success rate: {baseline.get('success_rate', 0.0):.2%}")
    lines.append(
        f"- Loops mean/median/total: {baseline.get('loops', {}).get('mean', 0.0):.2f} / "
        f"{baseline.get('loops', {}).get('median', 0.0):.2f} / "
        f"{baseline.get('loops', {}).get('total', 0.0):.2f}"
    )
    lines.append(
        f"- First-read hit rate: {baseline.get('first_read_hit_rate', 0.0):.2%} "
        f"(eligible tasks: {baseline.get('first_read_eligible_tasks', 0)})"
    )
    lines.append("")
    lines.append("## With Snapshot")
    lines.append("")
    lines.append(
        f"- Tokens mean/median/p90/total: {snapshot.get('tokens', {}).get('mean', 0.0):.2f} / "
        f"{snapshot.get('tokens', {}).get('median', 0.0):.2f} / "
        f"{snapshot.get('tokens', {}).get('p90', 0.0):.2f} / "
        f"{snapshot.get('tokens', {}).get('total', 0.0):.2f}"
    )
    lines.append(f"- Success rate: {snapshot.get('success_rate', 0.0):.2%}")
    lines.append(
        f"- Loops mean/median/total: {snapshot.get('loops', {}).get('mean', 0.0):.2f} / "
        f"{snapshot.get('loops', {}).get('median', 0.0):.2f} / "
        f"{snapshot.get('loops', {}).get('total', 0.0):.2f}"
    )
    lines.append(
        f"- First-read hit rate: {snapshot.get('first_read_hit_rate', 0.0):.2%} "
        f"(eligible tasks: {snapshot.get('first_read_eligible_tasks', 0)})"
    )
    lines.append("")
    lines.append("## Gate Thresholds")
    lines.append("")
    lines.append(f"- Min savings: {thresholds.get('min_savings', 0.0):.2%}")
    lines.append(f"- Max success drop: {thresholds.get('max_success_drop', 0.0):.2%}")
    lines.append(
        f"- Require non-increasing loops: {thresholds.get('require_non_increasing_loops', False)}"
    )

    if by_type:
        lines.append("")
        lines.append("## By Task Type (Delta)")
        lines.append("")
        for task_type, stats in by_type.items():
            lines.append(f"- {task_type}:")
            lines.append(
                f"  median_savings={stats.get('median_token_savings_rate', 0.0):.2%}, "
                f"total_savings={stats.get('total_token_savings_rate', 0.0):.2%}, "
                f"success_drop={stats.get('success_rate_drop', 0.0):.2%}, "
                f"loop_delta={stats.get('mean_loop_delta', 0.0):.3f}, "
                f"first_read_hit_delta={stats.get('first_read_hit_rate_delta', 0.0):.2%}"
            )

    lines.append("")
    lines.append("Report generated by kbgen benchmark.")
    return "\n".join(lines) + "\n"
