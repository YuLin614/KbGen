from __future__ import annotations

import json
import math
import re
from pathlib import Path
from typing import Any

from kbgen.constants import IGNORED_DIR_NAMES, SUPPORTED_EXTENSIONS

_MONOREPO_NAMESPACES = ("packages", "apps", "services", "libs")
_PACKAGE_MARKER_FILES = (
    "package.json",
    "pyproject.toml",
    "setup.py",
    "go.mod",
    "Cargo.toml",
)


def estimate_tokens(text: str) -> int:
    if not text:
        return 0
    coarse = math.ceil(len(text) / 4)
    punct = len(re.findall(r"[{}\[\](),:\".]", text)) // 4
    return max(1, coarse + punct)


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, separators=(",", ":"), ensure_ascii=True) + "\n", encoding="utf-8")


def normalize_module_root(value: str) -> str:
    normalized = value.strip().replace("\\", "/").strip("/")
    return normalized


def detect_monorepo_module_roots(root: Path) -> set[str]:
    roots: set[str] = set()
    for namespace in _MONOREPO_NAMESPACES:
        base = root / namespace
        if not base.exists() or not base.is_dir():
            continue
        for child in base.iterdir():
            if not child.is_dir() or child.name.startswith("."):
                continue
            if any((child / marker).exists() for marker in _PACKAGE_MARKER_FILES):
                roots.add(f"{namespace}/{child.name}")
    return roots


def list_namespace_module_roots(root: Path) -> set[str]:
    roots: set[str] = set()
    for namespace in _MONOREPO_NAMESPACES:
        base = root / namespace
        if not base.exists() or not base.is_dir():
            continue
        for child in base.iterdir():
            if child.is_dir() and not child.name.startswith("."):
                roots.add(f"{namespace}/{child.name}")
    return roots


def resolve_module_roots(
    root: Path,
    module_strategy: str = "auto",
    configured_module_roots: list[str] | None = None,
) -> set[str]:
    configured = {
        normalize_module_root(item)
        for item in (configured_module_roots or [])
        if normalize_module_root(item)
    }
    if configured:
        return configured

    if module_strategy == "monorepo_2level":
        return list_namespace_module_roots(root)

    if module_strategy == "auto":
        return detect_monorepo_module_roots(root)
    return set()


def module_for_path(
    path: Path,
    root: Path,
    module_roots: set[str] | None = None,
) -> str:
    rel = path.relative_to(root)
    parts = rel.parts
    if not parts:
        return "root"
    if len(parts) == 1:
        return "root"
    rel_posix = rel.as_posix()
    if module_roots:
        for module_root in sorted(module_roots, key=len, reverse=True):
            if rel_posix == module_root or rel_posix.startswith(module_root + "/"):
                return module_root
    first = parts[0]
    if first.startswith("."):
        return "root"
    return first


def collect_source_files(root: Path, extra_ignore: set[str] | None = None) -> list[Path]:
    ignore = IGNORED_DIR_NAMES | (extra_ignore or set())
    out: list[Path] = []
    for file in root.rglob("*"):
        if not file.is_file():
            continue
        if any(part in ignore for part in file.parts):
            continue
        if file.suffix.lower() in SUPPORTED_EXTENSIONS:
            out.append(file)
    return out


def parse_import_candidates(path: Path, text: str) -> list[str]:
    from kbgen.ast_parsers import get_parser
    parser = get_parser(path)
    if parser is None:
        return []
    return parser.extract_imports(text, path)


def extract_exports(path: Path, text: str) -> list[str]:
    from kbgen.ast_parsers import get_parser
    parser = get_parser(path)
    if parser is None:
        return []
    pairs = parser.extract_exports(text, path)
    return [name for name, _ in pairs][:10]


def extract_export_anchors(path: Path, text: str, root: Path) -> list[str]:
    from kbgen.ast_parsers import get_parser
    parser = get_parser(path)
    if parser is None:
        return []
    rel = path.relative_to(root).as_posix()
    pairs = parser.extract_exports(text, path)
    return [f"{name}@{rel}:{lineno}" for name, lineno in pairs][:10]


