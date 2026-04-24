from __future__ import annotations

import json
import subprocess
from datetime import date
from pathlib import Path
from typing import Any

from kbgen.analysis import ScanData, structural_scan, synthesize
from kbgen.constants import (
    DEFAULT_KEY_PATH_LIMIT,
    SCHEMA_TEXT,
    SUPPORTED_EXTENSIONS,
    TOOL_VERSION,
)
from kbgen.parsing import estimate_tokens, module_for_path, write_json
from kbgen.snapshot import build_snapshot, evict_snapshot


def build_meta(confidence: str, stale_modules: list[str]) -> dict[str, Any]:
    return {
        "generated_at": date.today().isoformat(),
        "tool_version": TOOL_VERSION,
        "coverage": "structure+semantics",
        "confidence": confidence,
        "stale_modules": stale_modules,
    }


def confidence_from_scan(scan: ScanData) -> str:
    if not scan.modules:
        return "low"
    if len(scan.modules) >= 3:
        return "medium"
    return "low"


def ensure_ai_dir(root: Path) -> Path:
    ai = root / ".ai"
    ai.mkdir(parents=True, exist_ok=True)
    return ai


def enforce_schema_budget(schema_text: str) -> None:
    _ = schema_text


def init_artifacts(root: Path) -> None:
    ai = ensure_ai_dir(root)
    enforce_schema_budget(SCHEMA_TEXT)

    schema_path = ai / "schema.kb"
    if (not schema_path.exists()) or schema_path.read_text(encoding="utf-8") != SCHEMA_TEXT:
        schema_path.write_text(SCHEMA_TEXT, encoding="utf-8")

    meta_path = ai / "meta.json"
    if not meta_path.exists():
        write_json(meta_path, build_meta(confidence="low", stale_modules=[]))


def full_scan(
    root: Path,
    key_path_limit: int = DEFAULT_KEY_PATH_LIMIT,
) -> dict[str, Any]:
    init_artifacts(root)
    scan = structural_scan(root)
    synthesized = synthesize(scan)
    snapshot = build_snapshot(synthesized, scan, key_path_limit=key_path_limit)
    snapshot = evict_snapshot(snapshot)

    ai = ensure_ai_dir(root)
    write_json(ai / "snapshot.kb", snapshot)
    write_json(ai / "meta.json", build_meta(confidence_from_scan(scan), stale_modules=[]))

    return {
        "modules": len(snapshot.get("m", {})),
        "tokens": estimate_tokens(json.dumps(snapshot, separators=(",", ":"), ensure_ascii=True)),
    }


def _read_snapshot(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def _git_changed_files(root: Path) -> list[str]:
    try:
        diff = subprocess.run(
            ["git", "diff", "--name-only", "HEAD"],
            cwd=root,
            check=False,
            capture_output=True,
            text=True,
        )
        changed = {line.strip().replace("\\", "/") for line in diff.stdout.splitlines() if line.strip()}

        untracked = subprocess.run(
            ["git", "ls-files", "--others", "--exclude-standard"],
            cwd=root,
            check=False,
            capture_output=True,
            text=True,
        )
        changed.update(
            line.strip().replace("\\", "/") for line in untracked.stdout.splitlines() if line.strip()
        )
        return sorted(changed)
    except FileNotFoundError:
        return []


def incremental_update(
    root: Path,
    key_path_limit: int = DEFAULT_KEY_PATH_LIMIT,
) -> dict[str, Any]:
    init_artifacts(root)
    ai = ensure_ai_dir(root)
    snapshot_path = ai / "snapshot.kb"
    previous = _read_snapshot(snapshot_path)

    scan = structural_scan(root)
    synthesized = synthesize(scan)
    current = build_snapshot(synthesized, scan, key_path_limit=key_path_limit)

    changed_files = _git_changed_files(root)
    changed_modules = {
        module_for_path(root / f, root)
        for f in changed_files
        if (root / f).suffix.lower() in SUPPORTED_EXTENSIONS
    }

    if not changed_modules:
        changed_modules = set(current.get("m", {}).keys()) - set(previous.get("m", {}).keys())

    merged = (
        json.loads(json.dumps(previous))
        if previous
        else {"m": {}, "f": [], "fd": [], "cy": [], "ri": [], "db": [], "ac": [], "no": [], "h": {}, "hf": {}, "hr": {}, "ls": []}
    )
    merged.setdefault("m", {})

    for module in changed_modules:
        if module in current.get("m", {}):
            merged["m"][module] = current["m"][module]

    existing_modules = set(current.get("m", {}).keys())
    for module in list(merged["m"].keys()):
        if module not in existing_modules:
            merged["m"].pop(module, None)

    merged["f"] = current.get("f", [])
    merged["fd"] = current.get("fd", [])
    merged["cy"] = current.get("cy", [])
    merged["ri"] = current.get("ri", [])
    merged["db"] = current.get("db", [])
    merged["ac"] = current.get("ac", [])
    merged["no"] = current.get("no", [])
    merged["h"] = current.get("h", {})
    merged["hf"] = current.get("hf", {})
    merged["hr"] = current.get("hr", {})
    merged["ls"] = current.get("ls", [])

    merged = evict_snapshot(merged)
    write_json(snapshot_path, merged)

    stale_modules = sorted(set(merged.get("m", {}).keys()) - changed_modules)
    write_json(ai / "meta.json", build_meta(confidence_from_scan(scan), stale_modules=stale_modules))

    return {
        "changed_modules": sorted(changed_modules),
        "modules": len(merged.get("m", {})),
        "tokens": estimate_tokens(json.dumps(merged, separators=(",", ":"), ensure_ascii=True)),
    }
