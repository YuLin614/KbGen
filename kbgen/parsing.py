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


def parse_import_candidates(text: str, suffix: str) -> list[str]:
    candidates: list[str] = []
    if suffix == ".py":
        for m in re.finditer(r"^\s*import\s+([a-zA-Z0-9_\.]+)", text, flags=re.MULTILINE):
            candidates.append(m.group(1))
        for m in re.finditer(r"^\s*from\s+([\.a-zA-Z0-9_]+)\s+import\s+", text, flags=re.MULTILINE):
            value = m.group(1)
            if value.startswith("."):
                candidates.append(value)
            else:
                candidates.append(value)
    elif suffix in {".js", ".jsx", ".ts", ".tsx"}:
        for m in re.finditer(r"from\s+[\"']([^\"']+)[\"']", text):
            target = m.group(1)
            candidates.append(target)
        for m in re.finditer(r"require\([\"']([^\"']+)[\"']\)", text):
            target = m.group(1)
            candidates.append(target)
    return candidates


def extract_exports(path: Path, text: str) -> list[str]:
    exports: set[str] = set()
    suffix = path.suffix.lower()
    if suffix == ".py":
        for m in re.finditer(r"^\s*def\s+([a-zA-Z_][a-zA-Z0-9_]*)\s*\(", text, flags=re.MULTILINE):
            exports.add(m.group(1))
        for m in re.finditer(r"^\s*class\s+([a-zA-Z_][a-zA-Z0-9_]*)\s*[:\(]", text, flags=re.MULTILINE):
            exports.add(m.group(1))
    elif suffix in {".js", ".jsx", ".ts", ".tsx"}:
        for m in re.finditer(r"export\s+(?:async\s+)?(?:function|class|const|let|var)\s+([a-zA-Z_][a-zA-Z0-9_]*)", text):
            exports.add(m.group(1))

        for m in re.finditer(
            r"export\s+default\s+(?:async\s+)?(?:function|class)\s+([a-zA-Z_][a-zA-Z0-9_]*)",
            text,
        ):
            exports.add(m.group(1))

        for m in re.finditer(r"export\s+default\s+([a-zA-Z_][a-zA-Z0-9_]*)\s*;", text):
            exports.add(m.group(1))

        for _ in re.finditer(r"export\s+default\s+(?:async\s+)?function\s*\(", text):
            exports.add("default")

    if not exports and suffix in {".js", ".jsx", ".ts", ".tsx"}:
        if re.search(r"\b(page|layout|route|loading|error|template)\.(jsx?|tsx?)$", path.name, flags=re.IGNORECASE):
            exports.add(path.stem)
    return sorted(exports)[:10]


def extract_export_anchors(path: Path, text: str, root: Path) -> list[str]:
    anchors: set[str] = set()
    suffix = path.suffix.lower()
    rel = path.relative_to(root).as_posix()

    if suffix == ".py":
        for m in re.finditer(r"^\s*def\s+([a-zA-Z_][a-zA-Z0-9_]*)\s*\(", text, flags=re.MULTILINE):
            line = text.count("\n", 0, m.start()) + 1
            anchors.add(f"{m.group(1)}@{rel}:{line}")
        for m in re.finditer(r"^\s*class\s+([a-zA-Z_][a-zA-Z0-9_]*)\s*[:\(]", text, flags=re.MULTILINE):
            line = text.count("\n", 0, m.start()) + 1
            anchors.add(f"{m.group(1)}@{rel}:{line}")
        for m in re.finditer(
            r"^\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*=\s*(?:Blueprint|APIRouter|Router)\s*\(",
            text,
            flags=re.MULTILINE,
        ):
            line = text.count("\n", 0, m.start()) + 1
            anchors.add(f"{m.group(1)}@{rel}:{line}")
    elif suffix in {".js", ".jsx", ".ts", ".tsx"}:
        seen_symbols: set[str] = set()

        def add_anchor(symbol: str, line: int) -> None:
            if symbol not in seen_symbols:
                seen_symbols.add(symbol)
                anchors.add(f"{symbol}@{rel}:{line}")

        pattern = r"export\s+(?:async\s+)?(?:function|class|const|let|var)\s+([a-zA-Z_][a-zA-Z0-9_]*)"
        for m in re.finditer(pattern, text):
            line = text.count("\n", 0, m.start()) + 1
            add_anchor(m.group(1), line)

        for m in re.finditer(
            r"export\s+default\s+(?:async\s+)?(?:function|class)\s+([a-zA-Z_][a-zA-Z0-9_]*)",
            text,
        ):
            line = text.count("\n", 0, m.start()) + 1
            add_anchor(m.group(1), line)

        for m in re.finditer(r"export\s+default\s+([a-zA-Z_][a-zA-Z0-9_]*)\s*;", text):
            symbol = m.group(1)
            decl = _find_js_symbol_declaration(text, symbol)
            pos = decl if decl >= 0 else m.start()
            line = text.count("\n", 0, pos) + 1
            add_anchor(symbol, line)

        for m in re.finditer(r"export\s+default\s+(?:async\s+)?function\s*\(", text):
            line = text.count("\n", 0, m.start()) + 1
            add_anchor("default", line)

        if not anchors:
            for symbol, pos in _find_component_like_symbols(text):
                line = text.count("\n", 0, pos) + 1
                add_anchor(symbol, line)
                if len(anchors) >= 6:
                    break

    if not anchors and suffix in {".js", ".jsx", ".ts", ".tsx"}:
        parts = {p.lower() for p in path.parts}
        if "app" in parts or "components" in parts:
            anchors.add(f"{path.stem}@{rel}:1")
    return sorted(anchors)[:10]


def _find_js_symbol_declaration(text: str, symbol: str) -> int:
    patterns = [
        rf"\bfunction\s+{re.escape(symbol)}\s*\(",
        rf"\bclass\s+{re.escape(symbol)}\b",
        rf"\b(?:const|let|var)\s+{re.escape(symbol)}\s*=",
    ]
    best = -1
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            pos = match.start()
            if best == -1 or pos < best:
                best = pos
    return best


def _find_component_like_symbols(text: str) -> list[tuple[str, int]]:
    out: list[tuple[str, int]] = []

    for m in re.finditer(r"\bfunction\s+([A-Z][a-zA-Z0-9_]*)\s*\(", text):
        out.append((m.group(1), m.start()))

    for m in re.finditer(r"\b(?:const|let|var)\s+([A-Z][a-zA-Z0-9_]*)\s*=\s*(?:\([^\)]*\)\s*=>|[a-zA-Z_][a-zA-Z0-9_]*\s*=>)", text):
        out.append((m.group(1), m.start()))

    for m in re.finditer(r"\b(?:const|let|var)\s+([A-Z][a-zA-Z0-9_]*)\s*:\s*[a-zA-Z0-9_\.<>]+\s*=", text):
        out.append((m.group(1), m.start()))

    seen: set[str] = set()
    unique: list[tuple[str, int]] = []
    for symbol, pos in sorted(out, key=lambda x: x[1]):
        if symbol in seen:
            continue
        seen.add(symbol)
        unique.append((symbol, pos))
    return unique
