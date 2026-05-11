from __future__ import annotations

import json
import subprocess
from datetime import datetime
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


def _git_changed_since(root: Path, since_ts: str) -> set[str] | None:
    """Return relative posix paths of source files changed since since_ts.

    Returns None if git is unavailable or fails (caller should use mtime fallback).
    Returns an empty set if git succeeded but found no changed files.
    """
    try:
        result = subprocess.run(
            ["git", "log", f"--since={since_ts}", "--name-only", "--format="],
            cwd=str(root),
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode != 0:
            return None
        changed: set[str] = set()
        for line in result.stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            if Path(line).suffix.lower() in SUPPORTED_EXTENSIONS:
                changed.add(line)
        return changed
    except Exception:
        return None


def _mtime_changed_since(root: Path, since_ts: str, snapshot_paths: set[str]) -> set[str]:
    """Fallback: compare file mtimes against since_ts ISO string."""
    try:
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
        git_result = _git_changed_since(root, generated_at)
        if git_result is None:
            # git unavailable — use mtime fallback
            changed = _mtime_changed_since(root, generated_at, snapshot_paths)
        else:
            changed = git_result
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
