from __future__ import annotations

import json
import math
import re
import subprocess
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

TOOL_VERSION = "kbgen@0.1"
DEFAULT_KEY_PATH_LIMIT = 0
SOFT_TOKEN_TARGET = 12000
SOFT_TOKEN_MAX = 22000

SCHEMA_TEXT = """Keys:
m = modules
r = role / responsibility
s = one-line module semantic
e = exports
a = export anchors (symbol@path:line)
d = depends_on
u = used_by
i = invariant / expectation
p = key file paths
f = directional flow (mostly acyclic)
fd = file dependency digest src>dst(count)
cy = mutual module dependency cycles (a<->b)
ri = route index summary
ac = auth chain summary
no = negative knowledge (discourage paths)
h = decision hints
hf = decision hint file targets
hr = machine-readable task read plan for hf targets
ls = loop sentinels (bias only)

hr format:
- each item: {"s":"S1|S2|S3...","t":"anchor_or_path","r":"reason_code"}

hr reason codes:
- EP_ENTRY = endpoint/route entrypoint
- EP_WRITE = endpoint write-flow core logic
- EP_VERIFY = endpoint test/verification target
- EP_NAV = endpoint-adjacent navigation fallback
- BUG_REPRO = bug reproduction/assertion target
- BUG_PATH = bug failure-path/guard logic
- BUG_HOT = bug hotspot fallback
- REF_SHARED = shared abstraction target
- REF_CORE = core logic target
- REF_STRUCT = structural cleanup target
- UI_ENTRY = UI component/page entrypoint
- UI_STATE = UI state/store/hook target
- UI_FLOW = UI interaction flow target
- AUTH_PATH = auth/session/token target
- NAV = generic navigation fallback

task keys may include ui_bugfix, ui_feature, ui_refactor when UI-focused modules are detected.

All entries are heuristic, not authoritative.
f may be empty when dependency direction cannot be inferred safely.
Use snapshot to guide WHERE to explore, not to replace reading code.
"""

IGNORED_DIR_NAMES = {
    ".git",
    "node_modules",
    ".venv",
    "venv",
    "dist",
    "build",
    ".ai",
    ".next",
    "coverage",
    "lcov-report",
    "out",
    "tmp",
}

SUPPORTED_EXTENSIONS = {
    ".py",
    ".js",
    ".jsx",
    ".ts",
    ".tsx",
    ".go",
    ".java",
    ".rb",
    ".rs",
    ".cs",
    ".php",
}

ENTRYPOINT_NAMES = {
    "main.py",
    "app.py",
    "index.js",
    "index.ts",
    "server.js",
    "server.ts",
    "manage.py",
}


def estimate_tokens(text: str) -> int:
    # Approximate token count without external dependencies.
    if not text:
        return 0
    coarse = math.ceil(len(text) / 4)
    punct = len(re.findall(r"[{}\[\](),:\".]", text)) // 4
    return max(1, coarse + punct)


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, separators=(",", ":"), ensure_ascii=True) + "\n", encoding="utf-8")


def module_for_path(path: Path, root: Path) -> str:
    rel = path.relative_to(root)
    parts = rel.parts
    if not parts:
        return "root"
    if len(parts) == 1:
        return "root"
    first = parts[0]
    if first.startswith("."):
        return "root"
    return first


def collect_source_files(root: Path) -> list[Path]:
    out: list[Path] = []
    for file in root.rglob("*"):
        if not file.is_file():
            continue
        if any(part in IGNORED_DIR_NAMES for part in file.parts):
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

        # Support default export forms common in React/Next.js.
        for m in re.finditer(
            r"export\s+default\s+(?:async\s+)?(?:function|class)\s+([a-zA-Z_][a-zA-Z0-9_]*)",
            text,
        ):
            exports.add(m.group(1))

        for m in re.finditer(r"export\s+default\s+([a-zA-Z_][a-zA-Z0-9_]*)\s*;", text):
            exports.add(m.group(1))

        for _ in re.finditer(r"export\s+default\s+(?:async\s+)?function\s*\(", text):
            exports.add("default")

    # TS/JSX files can still be high-value navigation files even when exports are implicit.
    if not exports and suffix in {".js", ".jsx", ".ts", ".tsx"}:
        if re.search(r"\b(page|layout|route|loading|error|template)\.(jsx?|tsx?)$", path.name, flags=re.IGNORECASE):
            exports.add(path.stem)
    return sorted(exports)[:10]


def infer_role(module: str, files: list[Path], has_entrypoint: bool) -> list[str]:
    tags: list[str] = []
    names = {f.name.lower() for f in files}
    joined = " ".join(names)
    suffixes = {f.suffix.lower() for f in files}
    is_python = ".py" in suffixes and ".ts" not in suffixes and ".tsx" not in suffixes
    if has_entrypoint:
        tags.append("entry")
    if any(k in module.lower() for k in ("api", "route", "http")) or "router" in joined:
        tags.append("routing")
    # Python backend: controllers and blueprints = routing
    if is_python and any(k in joined for k in ("controller", "blueprint", "views", "view")):
        if "routing" not in tags:
            tags.append("routing")
    # Python: Celery workers / tasks
    if is_python and any(k in joined for k in ("task", "worker", "celery", "consumer")):
        tags.append("worker")
    if any(k in module.lower() for k in ("auth", "login", "session")):
        tags.append("auth")
    if any(k in module.lower() for k in ("db", "repo", "model", "store")):
        tags.append("data")
    # Python: alembic migrations, models.py, schema.py = data
    if is_python and any(k in joined for k in ("model", "migration", "alembic", "schema", "orm")):
        if "data" not in tags:
            tags.append("data")
    if any(k in module.lower() for k in ("service", "svc", "domain", "logic")):
        tags.append("service")
    if any(k in module.lower() for k in ("test", "spec")) or any(k in joined for k in ("test_", "_test", "conftest")):
        tags.append("test")
    if not tags:
        tags.append("module")
    return tags[:3]


@dataclass
class ModuleSynthesis:
    role: list[str]
    summary: str
    exports: list[str]
    anchors: list[str]
    deps: list[str]
    used_by: list[str]
    invariants: list[str]


@dataclass
class ScanData:
    root: Path
    modules: dict[str, list[Path]]
    deps: dict[str, set[str]]
    entry_modules: set[str]
    exports: dict[str, list[str]]
    anchors: dict[str, list[str]]
    file_deps: list[tuple[str, str]]
    route_index: list[str]
    auth_chain: list[str]


def structural_scan(root: Path) -> ScanData:
    files = collect_source_files(root)
    modules: dict[str, list[Path]] = defaultdict(list)
    for f in files:
        modules[module_for_path(f, root)].append(f)

    exports: dict[str, list[str]] = defaultdict(list)
    anchors: dict[str, list[str]] = defaultdict(list)
    module_names = set(modules.keys())
    deps: dict[str, set[str]] = {m: set() for m in module_names}
    entry_modules: set[str] = set()
    file_deps: set[tuple[str, str]] = set()
    route_entries: set[str] = set()
    auth_markers: set[str] = set()

    # Build Python package-name → module-directory mapping from setup.py / pyproject.toml.
    # Enables resolving cross-service imports like `from dems_common.x import y` → module 'common'.
    pkg_to_module: dict[str, str] = {}
    for setup_file in root.rglob("setup.py"):
        if any(part in IGNORED_DIR_NAMES for part in setup_file.parts):
            continue
        try:
            setup_text = setup_file.read_text(encoding="utf-8", errors="ignore")
            for m in re.finditer(r"name\s*=\s*['\"]([^'\"]+)['\"]", setup_text):
                pkg_name = m.group(1).replace("-", "_")
                mod = module_for_path(setup_file, root)
                pkg_to_module[pkg_name] = mod
                break
        except Exception:
            pass
    for toml_file in root.rglob("pyproject.toml"):
        if any(part in IGNORED_DIR_NAMES for part in toml_file.parts):
            continue
        try:
            toml_text = toml_file.read_text(encoding="utf-8", errors="ignore")
            for m in re.finditer(r'^name\s*=\s*["\']([^"\']+)["\']', toml_text, flags=re.MULTILINE):
                pkg_name = m.group(1).replace("-", "_")
                mod = module_for_path(toml_file, root)
                pkg_to_module[pkg_name] = mod
                break
        except Exception:
            pass
    # Also detect installable packages by __init__.py inside top-level service dirs.
    for mod_name, mod_files in modules.items():
        for f in mod_files:
            if f.name == "__init__.py":
                # e.g. common/dems_common/__init__.py → pkg 'dems_common' → module 'common'
                pkg_candidate = f.parent.name
                if pkg_candidate not in module_names and pkg_candidate not in pkg_to_module:
                    pkg_to_module[pkg_candidate] = mod_name

    for module, module_files in modules.items():
        exp_names: set[str] = set()
        exp_anchors: set[str] = set()
        for file in module_files:
            text = file.read_text(encoding="utf-8", errors="ignore")
            src_rel = file.relative_to(root).as_posix()
            for name in extract_exports(file, text):
                exp_names.add(name)
            for anchor in extract_export_anchors(file, text, root):
                exp_anchors.add(anchor)
            for entry in extract_route_entries(file, text, root):
                route_entries.add(entry)
            for marker in extract_auth_markers(file, text, root):
                auth_markers.add(marker)

            if file.name in ENTRYPOINT_NAMES:
                entry_modules.add(module)

            for candidate in parse_import_candidates(text, file.suffix.lower()):
                resolved_module, resolved_path = resolve_import_target(
                    candidate, file, root, module_names, pkg_to_module
                )
                if resolved_module and resolved_module != module:
                    deps[module].add(resolved_module)
                if resolved_path is not None:
                    dst_rel = resolved_path.relative_to(root).as_posix()
                    if dst_rel != src_rel:
                        file_deps.add((src_rel, dst_rel))
        exports[module] = sorted(exp_names)[:12]
        anchors[module] = sorted(exp_anchors)  # no cap; rank_module_anchors caps at 12 for snapshot

    return ScanData(
        root=root,
        modules=dict(modules),
        deps=deps,
        entry_modules=entry_modules,
        exports=exports,
        anchors=anchors,
        file_deps=sorted(file_deps),
        route_index=sorted(route_entries)[:24],
        auth_chain=sorted(auth_markers)[:16],
    )


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
    elif suffix in {".js", ".jsx", ".ts", ".tsx"}:
        # Track seen symbols to handle TS function overloads — keep first occurrence only.
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

        # export default Component;
        for m in re.finditer(r"export\s+default\s+([a-zA-Z_][a-zA-Z0-9_]*)\s*;", text):
            symbol = m.group(1)
            decl = _find_js_symbol_declaration(text, symbol)
            pos = decl if decl >= 0 else m.start()
            line = text.count("\n", 0, pos) + 1
            add_anchor(symbol, line)

        # export default function (...) { ... }
        for m in re.finditer(r"export\s+default\s+(?:async\s+)?function\s*\(", text):
            line = text.count("\n", 0, m.start()) + 1
            add_anchor("default", line)

        # Fallback for component-heavy files that often omit explicit exports.
        if not anchors:
            for symbol, pos in _find_component_like_symbols(text):
                line = text.count("\n", 0, pos) + 1
                add_anchor(symbol, line)
                if len(anchors) >= 6:
                    break

    # In app/components trees, provide filename anchor as last-resort navigation.
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

    # function Component(...) { ... }
    for m in re.finditer(r"\bfunction\s+([A-Z][a-zA-Z0-9_]*)\s*\(", text):
        out.append((m.group(1), m.start()))

    # const Component = (...) => ...
    for m in re.finditer(r"\b(?:const|let|var)\s+([A-Z][a-zA-Z0-9_]*)\s*=\s*(?:\([^\)]*\)\s*=>|[a-zA-Z_][a-zA-Z0-9_]*\s*=>)", text):
        out.append((m.group(1), m.start()))

    # const Component: React.FC = ...
    for m in re.finditer(r"\b(?:const|let|var)\s+([A-Z][a-zA-Z0-9_]*)\s*:\s*[a-zA-Z0-9_\.<>]+\s*=", text):
        out.append((m.group(1), m.start()))

    # Deduplicate while preserving earlier positions.
    seen: set[str] = set()
    unique: list[tuple[str, int]] = []
    for symbol, pos in sorted(out, key=lambda x: x[1]):
        if symbol in seen:
            continue
        seen.add(symbol)
        unique.append((symbol, pos))
    return unique


def resolve_import_module(candidate: str, file: Path, root: Path, module_names: set[str]) -> str | None:
    module, _ = resolve_import_target(candidate, file, root, module_names)
    return module


def resolve_import_target(
    candidate: str,
    file: Path,
    root: Path,
    module_names: set[str],
    pkg_to_module: dict[str, str] | None = None,
) -> tuple[str | None, Path | None]:
    candidate = candidate.strip()
    if not candidate:
        return None, None

    if candidate.startswith("."):
        if file.suffix.lower() == ".py" and "/" not in candidate and "\\" not in candidate:
            resolved = resolve_relative_python_import(file, candidate, root)
        else:
            resolved = resolve_relative_import(file, candidate, root)
        if resolved is None:
            return None, None
        module = module_for_path(resolved, root)
        return (module if module in module_names else None), resolved

    # Common alias patterns: @/foo/bar, ~/foo/bar
    if candidate.startswith("@/") or candidate.startswith("~/"):
        resolved = resolve_absolute_like_import(root, candidate[2:])
        if resolved is not None:
            module = module_for_path(resolved, root)
            return (module if module in module_names else None), resolved

    # Common src root aliases.
    if candidate.startswith("src/"):
        resolved = resolve_absolute_like_import(root, candidate)
        if resolved is not None:
            module = module_for_path(resolved, root)
            return (module if module in module_names else None), resolved

    head = candidate.split("/")[0].split(".")[0]
    # Python: check package-name → module-directory mapping (e.g. dems_common → common).
    if pkg_to_module and head in pkg_to_module:
        target_module = pkg_to_module[head]
        if target_module in module_names:
            return target_module, None
    if head in module_names:
        resolved = resolve_absolute_like_import(root, candidate)
        if resolved is not None:
            return head, resolved
        return head, None
    return None, None


def resolve_absolute_like_import(root: Path, target: str) -> Path | None:
    stems = [(root / target).resolve()]
    if "/" not in target and "." in target:
        stems.append((root / target.replace(".", "/")).resolve())

    candidates: list[Path] = []
    for stem in stems:
        candidates.append(stem)
        for ext in (".py", ".ts", ".tsx", ".js", ".jsx"):
            candidates.append(stem.with_suffix(ext))
        for ext in (".py", ".ts", ".tsx", ".js", ".jsx"):
            candidates.append(stem / ("index" + ext))

    for c in candidates:
        if c.exists() and c.is_file() and root in c.parents:
            return c
    return None


def extract_route_entries(path: Path, text: str, root: Path) -> list[str]:
    suffix = path.suffix.lower()
    rel = path.relative_to(root).as_posix()

    # --- Python: Flask / FastAPI / Django routes ---
    if suffix == ".py":
        entries: list[str] = []
        # Flask: @bp.route('/path', methods=[...]) or @app.route(...)
        flask_pattern = re.compile(
            r"@[\w\.]+\.route\(['\"]([^'\"]+)['\"](?:[^)]*methods\s*=\s*\[([^\]]+)\])?",
        )
        for m in flask_pattern.finditer(text):
            route_path = m.group(1)
            methods_raw = m.group(2) or "GET"
            methods = re.findall(r"['\"]([A-Z]+)['\"]", methods_raw) or ["GET"]
            line = text.count("\n", 0, m.start()) + 1
            method_str = "|".join(sorted(methods))
            entries.append(f"api:{route_path}[{method_str}]->{rel}:{line}")
        # FastAPI: @router.get('/path') / @app.post(...) etc.
        fastapi_pattern = re.compile(
            r"@[\w\.]+\.(get|post|put|patch|delete|options|head)\(['\"]([^'\"]+)['\"]",
            re.IGNORECASE,
        )
        for m in fastapi_pattern.finditer(text):
            method = m.group(1).upper()
            route_path = m.group(2)
            line = text.count("\n", 0, m.start()) + 1
            entries.append(f"api:{route_path}[{method}]->{rel}:{line}")
        # Django: path('route/', view) / re_path
        django_pattern = re.compile(r"(?:re_)?path\(['\"]([^'\"]+)['\"]")
        for m in django_pattern.finditer(text):
            if "urlpatterns" in text or "include(" in text:
                route_path = m.group(1)
                line = text.count("\n", 0, m.start()) + 1
                entries.append(f"api:{route_path}->{rel}:{line}")
        return entries[:12]

    # --- JavaScript/TypeScript: Next.js app router ---
    if suffix not in {".js", ".jsx", ".ts", ".tsx"}:
        return []

    if not rel.startswith("app/"):
        return []

    stem = path.stem.lower()
    if stem not in {"page", "layout", "route", "loading", "error", "template"}:
        return []

    route = route_from_app_path(rel)
    line = first_line_match(
        text,
        [
            r"export\s+default",
            r"export\s+async\s+function\s+(GET|POST|PUT|PATCH|DELETE)",
            r"export\s+function\s+(GET|POST|PUT|PATCH|DELETE)",
        ],
    )

    kind = "api" if "/api/" in rel and stem == "route" else stem
    return [f"{kind}:{route}->{rel}:{line}"]


def route_from_app_path(rel_path: str) -> str:
    parts = rel_path.split("/")
    if not parts or parts[0] != "app":
        return "/"

    segments: list[str] = []
    for seg in parts[1:-1]:
        if not seg or (seg.startswith("(") and seg.endswith(")")) or seg.startswith("@"):
            continue
        if seg.startswith("[[...") and seg.endswith("]]"):
            segments.append(f":{seg[5:-2]}*")
            continue
        if seg.startswith("[...") and seg.endswith("]"):
            segments.append(f":{seg[4:-1]}*")
            continue
        if seg.startswith("[") and seg.endswith("]"):
            segments.append(f":{seg[1:-1]}")
            continue
        segments.append(seg)
    return "/" + "/".join(segments) if segments else "/"


def extract_auth_markers(path: Path, text: str, root: Path) -> list[str]:
    rel = path.relative_to(root).as_posix()
    lower = text.lower()
    markers: list[str] = []
    keyword_to_label = [
        ("keycloak", "keycloak"),
        ("oidc", "oidc"),
        ("openid", "openid"),
        ("nextauth", "nextauth"),
        ("oauth", "oauth"),
        ("authentication", "auth"),
        ("authorization", "authz"),
        ("access token", "access_token"),
        ("refresh token", "refresh_token"),
        ("middleware", "middleware"),
        ("jwt", "jwt"),
        ("session", "session"),
    ]

    auth_context = bool(re.search(r"\b(auth|oidc|oauth|openid|session|keycloak|nextauth|jwt|bearer)\b", lower))
    if not auth_context and not re.search(r"\b(auth|middleware)\b", rel.lower()):
        return []

    path_hint = bool(re.search(r"\b(auth|middleware|session|identity)\b", rel.lower()))
    op_context = re.compile(r"\b(import|from|require|create|init|login|logout|signin|authorize|session|middleware|token)\b")

    for keyword, label in keyword_to_label:
        pos = lower.find(keyword)
        if pos < 0:
            continue
        if not path_hint:
            left = max(0, pos - 120)
            right = min(len(lower), pos + 120)
            window = lower[left:right]
            if not op_context.search(window):
                continue
        line = text.count("\n", 0, pos) + 1
        markers.append(f"{label}@{rel}:{line}")
        break
    return markers


def first_line_match(text: str, patterns: list[str]) -> int:
    best: int | None = None
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            line = text.count("\n", 0, match.start()) + 1
            if best is None or line < best:
                best = line
    return best or 1


def resolve_relative_import(file: Path, target: str, root: Path) -> Path | None:
    base = (file.parent / target).resolve()
    candidates: list[Path] = [base]

    # Common extensionless import resolutions.
    for ext in (".py", ".ts", ".tsx", ".js", ".jsx"):
        candidates.append(base.with_suffix(ext))

    for ext in (".py", ".ts", ".tsx", ".js", ".jsx"):
        candidates.append(base / ("index" + ext))

    for c in candidates:
        if c.exists() and c.is_file() and root in c.parents:
            return c
    return None


def resolve_relative_python_import(file: Path, target: str, root: Path) -> Path | None:
    # target examples: ".utils", "..core", "."
    lead = len(target) - len(target.lstrip("."))
    tail = target.lstrip(".")

    base = file.parent
    if lead > 1:
        for _ in range(lead - 1):
            if root == base:
                break
            base = base.parent

    rel_parts = [p for p in tail.split(".") if p]
    stem = base.joinpath(*rel_parts) if rel_parts else base
    candidates = [stem, stem.with_suffix(".py"), stem / "__init__.py"]
    for c in candidates:
        if c.exists() and c.is_file() and root in c.parents:
            return c
    return None


def synthesize(scan: ScanData) -> dict[str, ModuleSynthesis]:
    used_by: dict[str, set[str]] = {m: set() for m in scan.modules}
    for src, targets in scan.deps.items():
        for target in targets:
            used_by[target].add(src)

    out: dict[str, ModuleSynthesis] = {}
    for module, files in scan.modules.items():
        role = infer_role(module, files, module in scan.entry_modules)
        deps = sorted(scan.deps.get(module, set()))
        inv: list[str] = []
        if module in scan.entry_modules:
            inv.append(f"{module}>entry")
        if "test" in role:
            inv.append(f"{module}>no_prod_path")

        out[module] = ModuleSynthesis(
            role=role,
            summary=infer_module_summary(module, role, files),
            exports=scan.exports.get(module, []),
            anchors=scan.anchors.get(module, []),
            deps=deps,
            used_by=sorted(used_by.get(module, set())),
            invariants=inv,
        )
    return out


def build_snapshot(
    synth: dict[str, ModuleSynthesis],
    scan: ScanData,
    key_path_limit: int = DEFAULT_KEY_PATH_LIMIT,
) -> dict[str, Any]:
    modules: dict[str, Any] = {}
    edges: list[list[str]] = []
    neg: list[str] = []
    # Full path→anchor lookup built before the [:12] cap, used by build_file_hints.
    path_to_best_anchor: dict[str, str] = {}

    for name, item in sorted(synth.items()):
        key_paths = module_key_paths(scan.root, scan.modules.get(name, []), item.exports, key_path_limit)
        ranked_anchors = rank_module_anchors(name, item.anchors)
        # Register all ranked anchors (full pool, not capped) for path lookup.
        for anchor in ranked_anchors:
            if "@" not in anchor:
                continue
            sym, rest = anchor.split("@", 1)
            anchor_path = rest.rsplit(":", 1)[0] if ":" in rest else rest
            if anchor_path not in path_to_best_anchor:
                # First occurrence wins (highest-ranked for that path).
                score = 2 if (sym and sym[0].isupper()) else 1
                existing = path_to_best_anchor.get(anchor_path)
                if existing is None:
                    path_to_best_anchor[anchor_path] = anchor
                else:
                    ex_sym = existing.split("@")[0]
                    ex_score = 2 if (ex_sym and ex_sym[0].isupper()) else 1
                    if score > ex_score:
                        path_to_best_anchor[anchor_path] = anchor
        modules[name] = {
            "r": item.role,
            "s": item.summary,
            "e": item.exports[:8],
            "a": ranked_anchors[:12],
            "d": item.deps,
            "u": item.used_by,
            "i": item.invariants,
            "p": key_paths,
        }
        for dep in item.deps:
            edges.append([name, dep])

        if "data" in item.role and "entry" in item.used_by:
            neg.append(f"{name}<-entry")
        if "auth" in item.role and "entry" in item.role:
            neg.append(f"{name}:split_entry_auth")

    if not neg and modules:
        # Ensure anti-loop signal exists even for sparse repos.
        first = sorted(modules.keys())[0]
        neg.append(f"{first}:avoid_blind_search")

    hints = build_hints(modules)
    file_hints = build_file_hints(modules, path_to_best_anchor)
    hint_rationales = build_hint_rationales(file_hints)
    module_cycles = detect_module_cycles(synth)
    if "app<->components" in module_cycles:
        neg.append("app<->components:check_boundary")
    ls = [
        "Prefer flow edges before global text search",
        "Validate one module before pivoting paths",
    ]

    snapshot: dict[str, Any] = {
        "m": modules,
        "f": sorted(edges)[:30],
        "fd": compress_file_deps(scan.file_deps, module_count=len(scan.modules)),
        "cy": module_cycles[:20],
        "ri": scan.route_index[:20],
        "ac": scan.auth_chain[:12],
        "no": sorted(set(neg))[:30],
        "h": hints,
        "hf": file_hints,
        "hr": hint_rationales,
        "ls": ls,
    }
    return snapshot


def build_hint_rationales(file_hints: dict[str, list[str]]) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = {}

    step_labels: dict[str, list[str]] = {
        "add_endpoint": ["S1", "S2", "S3"],
        "bugfix": ["S1", "S2", "S3"],
        "refactor": ["S1", "S2", "S3"],
        "ui_bugfix": ["S1", "S2", "S3"],
        "ui_feature": ["S1", "S2", "S3"],
        "ui_refactor": ["S1", "S2", "S3"],
        "auth_change": ["S1", "S2", "S3"],
    }

    def reason_for(task: str, target: str) -> str:
        lower = target.lower()

        if task == "add_endpoint":
            if any(k in lower for k in ("route", "api", "handler", "controller", "page", "action")):
                return "EP_ENTRY"
            if any(k in lower for k in ("service", "create", "post", "insert", "save")):
                return "EP_WRITE"
            if any(k in lower for k in ("test", "spec")):
                return "EP_VERIFY"
            return "EP_NAV"

        if task == "ui_feature":
            # App routes (Next.js pages/layouts) represent interaction flow
            if lower.startswith("app/") or "/page." in lower or "/layout." in lower:
                return "UI_FLOW"
            if any(k in lower for k in ("component", "view", "table", "panel", "dialog", "feature", "toolbar", "column")):
                return "UI_ENTRY"
            if any(k in lower for k in ("hook", "store", "state", "zustand", "use")):
                return "UI_STATE"
            return "UI_FLOW"

        if task == "bugfix":
            if any(k in lower for k in ("test", "spec", "assert")):
                return "BUG_REPRO"
            if any(k in lower for k in ("error", "exception", "validate", "check", "guard", "lock", "retry")):
                return "BUG_PATH"
            return "BUG_HOT"

        if task == "ui_bugfix":
            if any(k in lower for k in ("test", "spec", "assert")):
                return "BUG_REPRO"
            # App routes represent interaction flow
            if lower.startswith("app/") or "/page." in lower or "/layout." in lower:
                return "UI_FLOW"
            if any(k in lower for k in ("component", "view", "table", "panel", "dialog", "form")):
                return "UI_ENTRY"
            if any(k in lower for k in ("hook", "store", "state", "zustand", "use")):
                return "UI_STATE"
            return "UI_FLOW"

        if task in {"refactor", "ui_refactor"}:
            if any(k in lower for k in ("util", "helper", "type", "model", "repository", "adapter", "client", "mapper")):
                return "REF_SHARED"
            if any(k in lower for k in ("service", "domain", "core", "lib")):
                return "REF_CORE"
            return "REF_STRUCT"

        if any(k in lower for k in ("auth", "session", "token", "oauth", "oidc")):
            return "AUTH_PATH"
        return "NAV"

    for task, targets in file_hints.items():
        labels = step_labels.get(task, ["S1", "S2", "S3"])
        rows: list[dict[str, Any]] = []
        for idx, target in enumerate(targets[:6]):
            label = labels[idx] if idx < len(labels) else f"S{idx + 1}"
            rows.append({
                "s": label,
                "t": target,
                "r": reason_for(task, target),
            })
        out[task] = rows
    return out


def infer_module_summary(module: str, role: list[str], files: list[Path]) -> str:
    lower = module.lower()
    if lower == "app":
        return "Application routes, pages, and server handlers."
    if lower == "components":
        return "Reusable UI components and view composition units."
    if lower in {"test", "tests", "__tests__"} or lower.startswith("tests_"):
        return "Automated tests, fixtures, and behavior assertions."
    if lower == "hooks":
        return "Shared stateful hooks and lifecycle helpers."
    if lower == "services":
        return "Business operations and side-effect orchestration."
    if lower == "store":
        return "Client state containers and update actions."
    if lower == "lib":
        return "Cross-cutting utilities, adapters, and shared primitives."
    if lower == "types":
        return "Shared type contracts and shape definitions."
    if lower == "constants":
        return "Static configuration constants and domain enumerations."
    if lower == "public":
        return "Static assets and public runtime resources."
    if lower == "root":
        return "Top-level entrypoints, config, and cross-module glue code."
    # Python service-specific names
    if lower in {"controllers", "views", "blueprints"}:
        return "HTTP route handlers and request dispatch controllers."
    if lower in {"models", "model"}:
        return "ORM models and database schema definitions."
    if lower in {"schemas", "schema"}:
        return "Request/response validation schemas."
    if lower in {"tasks", "workers", "celery"}:
        return "Async task workers and background job definitions."
    if lower in {"migrations", "alembic"}:
        return "Database migration scripts and version history."
    if lower in {"utils", "helpers"}:
        return "Shared utility functions and helpers."
    if lower in {"common", "shared"}:
        return "Shared cross-service utilities and primitives."
    if lower.endswith("-service") or lower.endswith("_service"):
        return "Microservice root — contains app, models, controllers, and config."

    names = " ".join(f.name.lower() for f in files)
    if "test" in role:
        return "Test scaffolding and behavior verification."
    if "routing" in role or "route" in names or "controller" in names:
        return "HTTP or route entrypoints and request dispatch."
    if "worker" in role or "task" in names or "celery" in names:
        return "Async workers and background task execution."
    if "auth" in role:
        return "Authentication, session checks, and identity helpers."
    if "service" in role:
        return "Core use-cases and external integration workflows."
    if "data" in role:
        return "Data access and persistence boundaries."
    return "Module-level implementation and supporting utilities."


def detect_module_cycles(synth: dict[str, ModuleSynthesis]) -> list[str]:
    cycles: set[str] = set()
    for src, item in synth.items():
        for dst in item.deps:
            target = synth.get(dst)
            if target is None:
                continue
            if src in target.deps:
                a, b = sorted((src, dst))
                cycles.add(f"{a}<->{b}")
    return sorted(cycles)


def compress_file_deps(file_deps: list[tuple[str, str]], module_count: int = 1) -> list[str]:
    if not file_deps:
        return []
    counter = Counter(file_deps)
    ranked = sorted(counter.items(), key=lambda item: (-item[1], len(item[0][0]) + len(item[0][1]), item[0]))
    total_edges = len(ranked)
    base_limit = max(24, module_count * 6)
    adaptive_limit = min(180, max(base_limit, int(total_edges * 0.45)))
    out: list[str] = []
    for (src, dst), count in ranked[:adaptive_limit]:
        out.append(f"{src}>{dst}({count})")
    return out


def rank_module_anchors(module: str, anchors: list[str]) -> list[str]:
    # Deduplicate while preserving order (in case of upstream bugs).
    seen_anchors: set[str] = set()
    unique: list[str] = []
    for a in anchors:
        if a not in seen_anchors:
            seen_anchors.add(a)
            unique.append(a)
    anchors = unique

    if not anchors:
        return []

    shadcn_primitives = {
        "accordion", "alert", "avatar", "badge", "button", "calendar", "card",
        "checkbox", "dialog", "drawer", "dropdownmenu", "input", "label", "popover",
        "radio", "select", "separator", "sheet", "skeleton", "slider", "switch",
        "table", "tabs", "textarea", "toast", "tooltip",
    }

    def split_anchor(anchor: str) -> tuple[str, str, int]:
        if "@" not in anchor:
            return anchor, "", 1
        symbol, rest = anchor.split("@", 1)
        if ":" in rest:
            path, line = rest.rsplit(":", 1)
            try:
                return symbol, path, int(line)
            except ValueError:
                return symbol, path, 1
        return symbol, rest, 1

    def is_ui_primitive_anchor(anchor: str) -> bool:
        symbol, path, _ = split_anchor(anchor)
        lower_sym = symbol.strip().lower()
        lower_path = path.lower()
        in_ui_tree = "/ui/" in lower_path or lower_path.startswith("components/ui/")
        return in_ui_tree or (lower_sym in shadcn_primitives)

    def score(anchor: str) -> tuple[int, int, str]:
        symbol, path, line = split_anchor(anchor)
        sym = symbol.strip()
        lower_sym = sym.lower()
        lower_path = path.lower()
        points = 0

        if module == "components":
            if "/ui/" in lower_path or lower_path.startswith("components/ui/"):
                points -= 3
            if lower_sym in shadcn_primitives:
                points -= 3
            if any(k in sym for k in ("Table", "Panel", "List", "Row", "Column", "Form", "Chart", "Audit", "File")):
                points += 4
            if any(k in lower_path for k in ("feature", "module", "domain", "container", "panel", "table")):
                points += 3

        if module == "app":
            if any(k in lower_path for k in ("/page.", "/layout.", "/route.", "/loading.", "/error.")):
                points += 3

        return (-points, len(path), f"{line:06d}:{anchor}")

    ranked = sorted(anchors, key=score)

    # In component-heavy repos, keep anchors focused on feature/business components.
    # Drop shadcn primitives entirely whenever any business anchor is available.
    if module == "components":
        business = [anchor for anchor in ranked if not is_ui_primitive_anchor(anchor)]
        if len(business) >= 1:
            return business  # drop shadcn primitives entirely

    return ranked


def module_key_paths(root: Path, files: list[Path], exports: list[str], limit: int) -> list[str]:
    if not files:
        return []

    export_bias = 1 if exports else 0

    def score(path: Path) -> tuple[int, int, str]:
        name = path.name.lower()
        points = 0
        if name in ENTRYPOINT_NAMES:
            points += 5
        if any(k in name for k in ("route", "router", "api", "service", "handler", "test")):
            points += 2
        if path.suffix.lower() in {".py", ".ts", ".tsx", ".js", ".jsx"}:
            points += 1
        points += export_bias
        rel = path.relative_to(root).as_posix()
        return (-points, len(rel), rel)

    ordered = sorted(files, key=score)
    if limit <= 0:
        return [p.relative_to(root).as_posix() for p in ordered]
    return [p.relative_to(root).as_posix() for p in ordered[:limit]]


def build_hints(modules: dict[str, Any]) -> dict[str, list[str]]:
    names = sorted(modules.keys())
    if not names:
        return {"bootstrap": ["root"]}

    api_like = [n for n in names if "routing" in modules[n].get("r", [])]
    service_like = [n for n in names if "service" in modules[n].get("r", [])]
    worker_like = [n for n in names if "worker" in modules[n].get("r", [])]
    test_like = [n for n in names if "test" in modules[n].get("r", [])]
    # Exclude Celery workers from endpoint-adjacent hints
    service_no_worker = [n for n in service_like if n not in worker_like]

    hints: dict[str, list[str]] = {}
    hints["add_endpoint"] = (api_like[:1] + service_no_worker[:1] + test_like[:1]) or names[:2]
    hints["bugfix"] = (service_no_worker[:1] + api_like[:1] + names[:1])[:2] or names[:2]
    hints["refactor"] = (service_no_worker[:1] + names[:2])[:2] or names[:2]
    ui_like = [
        n for n in names
        if any(k in n.lower() for k in ("component", "ui", "view", "page", "layout", "hook", "store", "frontend", "client"))
    ]
    if ui_like:
        hints["ui_bugfix"] = (ui_like[:2] + test_like[:1])[:2] or ui_like[:2]
        hints["ui_feature"] = (ui_like[:2] + api_like[:1])[:2] or ui_like[:2]
        hints["ui_refactor"] = ui_like[:2]
    if any("auth" in modules[n].get("r", []) for n in names):
        auth = [n for n in names if "auth" in modules[n].get("r", [])]
        hints["auth_change"] = auth[:2]
    else:
        hints["module_change"] = names[:2]
    return hints


def build_file_hints(modules: dict[str, Any], path_to_best_anchor: dict[str, str] | None = None) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}

    def pick_paths(module_names: list[str], limit: int = 3) -> list[str]:
        paths: list[str] = []
        for mod in module_names:
            for path in modules.get(mod, {}).get("p", []):
                if path not in paths:
                    paths.append(path)
                if len(paths) >= limit:
                    return paths
        return paths

    def pick_ui_paths_by_keywords(
        module_names: list[str],
        keywords: list[str],
        avoid: set[str],
        limit: int = 3,
    ) -> list[str]:
        # Normalize avoid: extract bare paths from anchor strings so bare-path
        # comparison works even when avoid contains symbol@path:line entries.
        avoid_paths: set[str] = set()
        for entry in avoid:
            if "@" in entry:
                _, rest = entry.split("@", 1)
                avoid_paths.add(rest.rsplit(":", 1)[0] if ":" in rest else rest)
            else:
                avoid_paths.add(entry)
        candidates: list[tuple[int, int, str]] = []
        seen: set[str] = set()
        for mod in module_names:
            for path in modules.get(mod, {}).get("p", []):
                if path in seen or path in avoid_paths:
                    continue
                seen.add(path)
                lower = path.lower()
                points = 0
                if "components/features/" in lower:
                    points += 6
                if "components/" in lower:
                    points += 2
                for kw in keywords:
                    if kw in lower:
                        points += 4
                candidates.append((points, -len(path), path))

        candidates.sort(key=lambda item: (-item[0], -item[1], item[2]))
        return [best_anchor_for_path(path) for _, _, path in candidates[:limit]]

    def merge_targets(primary: list[str], supplement: list[str], limit: int = 3) -> list[str]:
        out_targets: list[str] = []
        for target in primary + supplement:
            if target in out_targets:
                continue
            out_targets.append(target)
            if len(out_targets) >= limit:
                break
        return out_targets

    def best_anchor_for_path(path: str) -> str:
        """Upgrade a bare path to its best anchor (symbol@path:line) if one exists."""
        # Use the full pre-cap lookup first.
        if path_to_best_anchor and path in path_to_best_anchor:
            return path_to_best_anchor[path]
        # Fallback: search capped module anchors.
        candidates: list[tuple[int, str]] = []
        for mod_data in modules.values():
            for anchor in mod_data.get("a", []):
                if "@" not in anchor:
                    continue
                sym, rest = anchor.split("@", 1)
                anchor_path = rest.rsplit(":", 1)[0] if ":" in rest else rest
                if anchor_path != path:
                    continue
                score = 2 if (sym and sym[0].isupper()) else 1
                candidates.append((score, anchor))
        if candidates:
            candidates.sort(key=lambda x: (-x[0], x[1]))
            return candidates[0][1]
        return path

    route_mods = [m for m in sorted(modules) if "routing" in modules[m].get("r", [])]
    service_mods = [m for m in sorted(modules) if "service" in modules[m].get("r", [])]
    worker_mods = [m for m in sorted(modules) if "worker" in modules[m].get("r", [])]
    test_mods = [m for m in sorted(modules) if "test" in modules[m].get("r", [])]
    data_mods = [m for m in sorted(modules) if "data" in modules[m].get("r", [])]
    # Exclude worker modules from service_mods for endpoint-related tasks
    service_mods_no_worker = [m for m in service_mods if m not in worker_mods]
    ui_mods = [
        m for m in sorted(modules)
        if any(k in m.lower() for k in ("component", "ui", "view", "page", "layout", "hook", "store", "frontend", "client"))
    ]
    anchor_to_module: dict[str, str] = {}

    def split_anchor(anchor: str) -> tuple[str, str]:
        if "@" not in anchor:
            return anchor.lower(), ""
        symbol, rest = anchor.split("@", 1)
        path = rest.rsplit(":", 1)[0] if ":" in rest else rest
        return symbol.lower(), path.lower()

    def score_anchor(anchor: str, module_name: str, task: str) -> tuple[int, int, str]:
        symbol, path = split_anchor(anchor)
        text = f"{symbol} {path}"
        points = 0

        if task == "add_endpoint":
            if module_name in route_mods:
                points += 5
            if module_name in service_mods:
                points += 2
            # Path-based boost: controller/blueprint files are primary HTTP entry points
            for kw in ("controller", "blueprint", "views", "/api/", "router"):
                if kw in path:
                    points += 6
            # Penalise health checks, base templates, and generic infra files
            for kw in ("health", "ping", "flask-base", "flask_base"):
                if kw in path:
                    points -= 10
            # Penalise app factory, middleware, and infra files — not where endpoints live
            for kw in ("/__init__.", "/handlers.", "/provider.", "/ses.", "/s3.", "/db."):
                if kw in path:
                    points -= 8
            for kw in ("route", "api", "endpoint", "handler", "controller", "post", "put", "patch", "delete", "blueprint", "view"):
                if kw in text:
                    points += 3
            for kw in ("create", "insert", "save", "update", "list", "get"):
                if kw in text:
                    points += 2
            for kw in ("file", "record", "document", "item", "entity", "crud"):
                if kw in text:
                    points += 4
            # Penalise workers/tasks — they are not HTTP endpoint entry points
            for kw in ("worker", "task", "celery", "run_worker", "consumer", "make_celery"):
                if kw in text:
                    points -= 8
            # Penalise DB/infra files — not where you define endpoints
            for kw in ("/db.", "/s3.", "/models.", "/migration", "/alembic", "/schema.", "setup_db", "setup_s3"):
                if kw in path:
                    points -= 10
            for kw in ("auth", "signin", "signout", "logout", "login", "session", "oauth", "oidc", "token"):
                if kw in text:
                    points -= 6

        if task == "bugfix":
            if module_name in service_mods:
                points += 4
            if module_name in route_mods:
                points += 2
            if module_name in test_mods:
                points += 2
            if module_name in ui_mods:
                points += 3
            for kw in ("error", "exception", "validate", "check", "retry", "lock", "cache", "fail", "guard"):
                if kw in text:
                    points += 3
            for kw in ("test", "spec", "assert"):
                if kw in text:
                    points += 2
            for kw in ("component", "view", "table", "panel", "dialog", "form", "button", "card", "list", "grid", "use"):
                if kw in text:
                    points += 2

        if task == "refactor":
            if module_name in service_mods or module_name in data_mods:
                points += 4
            if module_name in ui_mods:
                points += 3
            for kw in ("util", "helper", "base", "adapter", "client", "repository", "model", "type", "mapper", "transform"):
                if kw in text:
                    points += 3
            for kw in ("component", "view", "table", "panel", "dialog", "hook", "store"):
                if kw in text:
                    points += 2
            for kw in ("route", "api", "endpoint"):
                if kw in text:
                    points -= 2

        if task == "ui_bugfix":
            if module_name in ui_mods:
                points += 7
            if module_name in service_mods or module_name in data_mods:
                points -= 3
            for kw in ("component", "view", "table", "panel", "dialog", "modal", "form", "button", "card", "list", "grid", "layout", "page"):
                if kw in text:
                    points += 4
            for kw in ("hook", "store", "state", "zustand", "use"):
                if kw in text:
                    points += 3
            for kw in ("error", "bug", "fix", "fallback", "loading", "empty", "lock", "warning"):
                if kw in text:
                    points += 3
            for kw in ("feature", "create", "new", "add"):
                if kw in text:
                    points -= 2
            for kw in ("service", "client", "repository", "api", "endpoint"):
                if kw in text:
                    points -= 3

        if task == "ui_feature":
            if module_name in ui_mods:
                points += 8
            if module_name in route_mods:
                points += 2
            if module_name in service_mods or module_name in data_mods:
                points -= 4
            for kw in ("component", "view", "table", "panel", "dialog", "form", "card", "layout", "page", "feature"):
                if kw in text:
                    points += 4
            for kw in ("filetable", "file-view", "fileview", "file-lock", "filelock", "lock-dialog", "lockdialog"):
                if kw in text:
                    points += 5
            for kw in ("hook", "store", "state", "zustand", "use"):
                if kw in text:
                    points += 3
            for kw in ("feature", "create", "new", "add", "upload", "filter", "search", "sort", "column", "toolbar"):
                if kw in text:
                    points += 3
            for kw in ("error", "bug", "fix", "fallback"):
                if kw in text:
                    points -= 2
            for kw in ("service", "client", "repository", "api", "endpoint", "create", "insert", "save"):
                if kw in text:
                    points -= 3

        return (points, -len(path), anchor)

    def collect_scored_anchors(module_order: list[str], task: str) -> list[str]:
        scored: list[tuple[int, int, str]] = []
        seen: set[str] = set()
        for mod in module_order:
            for anchor in modules.get(mod, {}).get("a", []):
                if anchor in seen:
                    continue
                seen.add(anchor)
                anchor_to_module[anchor] = mod
                scored.append(score_anchor(anchor, mod, task))
        scored.sort(key=lambda item: (-item[0], -item[1], item[2]))
        return [anchor for _, _, anchor in scored]

    def pick_task_hints(
        task: str,
        module_order: list[str],
        fallback_modules: list[str],
        avoid: set[str],
        require_ui_mix: bool = False,
        strict_avoid: bool = False,
    ) -> list[str]:
        ranked = collect_scored_anchors(module_order, task)

        if task == "add_endpoint":
            # Prefer endpoint-like paths so controller files are not lost by
            # capped per-module anchor lists.
            endpoint_path_first: list[str] = []
            seen_paths: set[str] = set()
            include_kws = ("/controllers/", "controller", "blueprint", "/api/", "/routes", "/route", "endpoint", "views")
            exclude_kws = (
                "/tests/", "/test_", "/alembic", "/migration", "/db.",
                "/s3.", "/ses.", "/provider.", "/handlers.", "/__init__.",
                "run_worker", "worker",
            )
            for mod in module_order:
                for path in modules.get(mod, {}).get("p", []):
                    lower = path.lower()
                    if lower in seen_paths:
                        continue
                    seen_paths.add(lower)
                    if not any(kw in lower for kw in include_kws):
                        continue
                    if any(kw in lower for kw in exclude_kws):
                        continue
                    endpoint_path_first.append(best_anchor_for_path(path))

            if endpoint_path_first:
                merged_ranked: list[str] = []
                seen_ranked: set[str] = set()
                for anchor in endpoint_path_first + ranked:
                    if anchor in seen_ranked:
                        continue
                    seen_ranked.add(anchor)
                    merged_ranked.append(anchor)
                ranked = merged_ranked

        selected: list[str] = []

        for anchor in ranked:
            if anchor in avoid:
                continue
            selected.append(anchor)
            if len(selected) >= 3:
                break

        if not selected:
            return pick_paths(fallback_modules)

        if len(selected) < 3:
            for anchor in ranked:
                if strict_avoid and anchor in avoid:
                    continue
                if anchor in selected:
                    continue
                selected.append(anchor)
                if len(selected) >= 3:
                    break

        if require_ui_mix and ui_mods and selected:
            has_ui = any(anchor_to_module.get(anchor, "") in ui_mods for anchor in selected)
            if not has_ui:
                ui_candidate = next(
                    (
                        anchor for anchor in ranked
                        if anchor_to_module.get(anchor, "") in ui_mods and anchor not in selected and anchor not in avoid
                    ),
                    None,
                )
                if ui_candidate is None:
                    ui_candidate = next(
                        (
                            anchor for anchor in ranked
                            if anchor_to_module.get(anchor, "") in ui_mods and anchor not in selected
                        ),
                        None,
                    )
                if ui_candidate is not None:
                    if len(selected) >= 3:
                        selected[-1] = ui_candidate
                    else:
                        selected.append(ui_candidate)

        if selected:
            return selected[:3]

        return pick_paths(fallback_modules)

    non_test_mods = [m for m in sorted(modules) if "test" not in modules[m].get("r", [])]
    routing_or_service = route_mods + [
        m for m in service_mods_no_worker if m not in route_mods
    ] if route_mods or service_mods_no_worker else non_test_mods
    add_endpoint_hints = pick_task_hints(
        "add_endpoint",
        routing_or_service,
        routing_or_service,
        set(),
    )
    bugfix_hints = pick_task_hints(
        "bugfix",
        service_mods_no_worker + ui_mods + route_mods + test_mods + sorted(modules),
        ui_mods + service_mods_no_worker + route_mods + sorted(modules),
        set(add_endpoint_hints),
        require_ui_mix=True,
    )
    refactor_hints = pick_task_hints(
        "refactor",
        service_mods_no_worker + data_mods + ui_mods + sorted(modules),
        ui_mods + service_mods_no_worker + data_mods + sorted(modules),
        set(add_endpoint_hints + bugfix_hints),
        require_ui_mix=True,
    )
    out["add_endpoint"] = add_endpoint_hints
    out["bugfix"] = bugfix_hints
    out["refactor"] = refactor_hints

    if ui_mods:
        ui_bugfix_base = pick_task_hints(
            "ui_bugfix",
            ui_mods + test_mods + service_mods + sorted(modules),
            ui_mods + test_mods + sorted(modules),
            set(add_endpoint_hints + bugfix_hints),
            require_ui_mix=True,
            strict_avoid=True,
        )
        ui_bugfix_paths = pick_ui_paths_by_keywords(
            ui_mods,
            ["file-lock-dialog", "lock-dialog", "file-table", "file-view", "dialog", "error", "warning"],
            set(add_endpoint_hints + bugfix_hints),
            limit=2,
        )
        out["ui_bugfix"] = merge_targets(ui_bugfix_paths, ui_bugfix_base, limit=3)

        ui_feature_base = pick_task_hints(
            "ui_feature",
            ui_mods + route_mods + service_mods + sorted(modules),
            ui_mods + route_mods + sorted(modules),
            set(add_endpoint_hints + bugfix_hints + out["ui_bugfix"]),
            require_ui_mix=True,
            strict_avoid=True,
        )
        ui_feature_paths = pick_ui_paths_by_keywords(
            ui_mods,
            ["file-table", "file-view", "file-lock-dialog", "columns", "toolbar", "media-viewer", "upload", "feature"],
            set(add_endpoint_hints + bugfix_hints + out["ui_bugfix"]),
            limit=2,
        )
        out["ui_feature"] = merge_targets(ui_feature_paths, ui_feature_base, limit=3)

        out["ui_refactor"] = pick_task_hints(
            "refactor",
            ui_mods + data_mods + service_mods + sorted(modules),
            ui_mods + sorted(modules),
            set(add_endpoint_hints + bugfix_hints + refactor_hints + out["ui_bugfix"] + out["ui_feature"]),
            require_ui_mix=True,
            strict_avoid=True,
        )

    auth_mods = [m for m in sorted(modules) if "auth" in modules[m].get("r", [])]
    if auth_mods:
        auth_paths = pick_paths(auth_mods)
        auth_anchors: list[str] = []
        for mod in auth_mods:
            for anchor in modules.get(mod, {}).get("a", []):
                if anchor not in auth_anchors:
                    auth_anchors.append(anchor)
                if len(auth_anchors) >= 3:
                    break
            if len(auth_anchors) >= 3:
                break
        out["auth_change"] = auth_anchors or auth_paths

    return out


def ensure_ai_dir(root: Path) -> Path:
    ai = root / ".ai"
    ai.mkdir(parents=True, exist_ok=True)
    return ai


def enforce_schema_budget(schema_text: str) -> None:
    # Token budgeting has been removed to preserve full schema detail.
    _ = schema_text


def evict_snapshot(snapshot: dict[str, Any]) -> dict[str, Any]:
    candidate = json.loads(json.dumps(snapshot))

    candidate = compact_snapshot(candidate)

    if not candidate.get("no"):
        candidate["no"] = ["root:avoid_blind_search"]
    if not candidate.get("h"):
        candidate["h"] = {"bootstrap": ["root"]}

    return candidate


def compact_snapshot(snapshot: dict[str, Any]) -> dict[str, Any]:
    candidate = json.loads(json.dumps(snapshot))

    def token_size(payload: dict[str, Any]) -> int:
        return estimate_tokens(json.dumps(payload, separators=(",", ":"), ensure_ascii=True))

    size = token_size(candidate)
    if size <= SOFT_TOKEN_TARGET:
        return candidate

    scale = max(0.25, min(1.0, SOFT_TOKEN_TARGET / max(1, size)))

    modules = candidate.get("m", {})
    for mod in modules.values():
        paths = mod.get("p", [])
        if isinstance(paths, list) and len(paths) > 0:
            keep = max(8, int(len(paths) * scale))
            mod["p"] = paths[:keep]

    fd = candidate.get("fd", [])
    if isinstance(fd, list) and len(fd) > 0:
        keep_fd = max(24, int(len(fd) * scale))
        candidate["fd"] = fd[:keep_fd]

    # Extreme fallback only for very large snapshots: keep navigation core while
    # bounding path/dependency fanout.
    if token_size(candidate) > SOFT_TOKEN_MAX:
        for mod in modules.values():
            paths = mod.get("p", [])
            if isinstance(paths, list):
                mod["p"] = paths[:12]
        fd = candidate.get("fd", [])
        if isinstance(fd, list):
            candidate["fd"] = fd[:48]

    return candidate


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
        # Nothing explicit changed; keep snapshot in sync for newly discovered modules.
        changed_modules = set(current.get("m", {}).keys()) - set(previous.get("m", {}).keys())

    merged = (
        json.loads(json.dumps(previous))
        if previous
        else {"m": {}, "f": [], "fd": [], "cy": [], "ri": [], "ac": [], "no": [], "h": {}, "hf": {}, "hr": {}, "ls": []}
    )
    merged.setdefault("m", {})

    for module in changed_modules:
        if module in current.get("m", {}):
            merged["m"][module] = current["m"][module]

    existing_modules = set(current.get("m", {}).keys())
    for module in list(merged["m"].keys()):
        if module not in existing_modules:
            merged["m"].pop(module, None)

    # Keep global anti-loop and navigation signals current.
    merged["f"] = current.get("f", [])
    merged["fd"] = current.get("fd", [])
    merged["cy"] = current.get("cy", [])
    merged["ri"] = current.get("ri", [])
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
