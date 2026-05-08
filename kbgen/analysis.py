from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

from kbgen.config import KbgenConfig, load_config
from kbgen.constants import ENTRYPOINT_NAMES, IGNORED_DIR_NAMES, ROUTE_INDEX_LIMIT
from kbgen.import_resolver import resolve_import_target
from kbgen.parsing import (
    collect_source_files,
    extract_export_anchors,
    extract_exports,
    module_for_path,
    parse_import_candidates,
    resolve_module_roots,
)
from kbgen.route_extraction import extract_auth_markers, extract_route_entries


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


def infer_role(module: str, files: list[Path], has_entrypoint: bool, config: KbgenConfig) -> list[str]:
    tags: list[str] = []
    names = {f.name.lower() for f in files}
    joined = " ".join(names)
    suffixes = {f.suffix.lower() for f in files}
    is_python = ".py" in suffixes and ".ts" not in suffixes and ".tsx" not in suffixes
    lower = module.lower()

    if has_entrypoint:
        tags.append("entry")

    # profile role_map: exact module name match takes priority
    if lower in config.role_map:
        mapped = config.role_map[lower]
        if mapped not in tags:
            tags.append(mapped)

    if any(k in lower for k in ("api", "route", "http")) or "router" in joined:
        if "routing" not in tags:
            tags.append("routing")
    if is_python and any(k in joined for k in ("controller", "blueprint", "views", "view")):
        if "routing" not in tags:
            tags.append("routing")
    if is_python and any(k in joined for k in ("task", "worker", "celery", "consumer")):
        if "worker" not in tags:
            tags.append("worker")
    if any(k in lower for k in ("auth", "login", "session")):
        if "auth" not in tags:
            tags.append("auth")
    if any(k in lower for k in ("db", "repo", "model", "store")):
        if "data" not in tags:
            tags.append("data")
    if is_python and any(k in joined for k in ("model", "migration", "alembic", "schema", "orm")):
        if "data" not in tags:
            tags.append("data")
    if any(k in lower for k in ("service", "svc", "domain", "logic")):
        if "service" not in tags:
            tags.append("service")
    if any(k in lower for k in ("test", "spec")) or any(k in joined for k in ("test_", "_test", "conftest")):
        if "test" not in tags:
            tags.append("test")
    if not tags:
        tags.append("module")
    return tags[:3]


def infer_module_summary(module: str, role: list[str], files: list[Path], config: KbgenConfig) -> str:
    lower = module.lower()

    # profile/user summary override
    if lower in config.summaries:
        return config.summaries[lower]

    names = " ".join(f.name.lower() for f in files)
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
        facets: list[str] = []
        auth_hits = names.count("auth") + names.count("jwt") + names.count("keycloak")
        audit_hits = names.count("audit")
        if "auth" in lower or "auth" in role:
            facets.append("auth and identity — central authorization authority for all services")
        if "audit" in lower or audit_hits >= 2:
            facets.append("audit logging")
        if "record" in lower or "record" in names or "file" in names:
            facets.append("record and file lifecycle")
        if "notification" in lower or any(k in names for k in ("preference", "policy", "event_type", "channel")):
            facets.append("notification policies and preferences")
        if "retention" in names:
            facets.append("retention workflows")
        if "share" in names:
            facets.append("sharing and access")
        if "dlq" in names or "dead_letter" in names:
            facets.append("DLQ replay and recovery")
        if "worker" in names or "celery" in names:
            facets.append("background workers")
        if "db" in names or "dao" in names or "model" in names or "alembic" in names:
            facets.append("persistence")
        if facets:
            unique_facets = list(dict.fromkeys(facets))
            return "Microservice root focused on " + ", ".join(unique_facets[:3]) + "."
        return "Microservice root — contains app, models, controllers, and config."

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


def infer_module_invariants(module: str, role: list[str], files: list[Path], config: KbgenConfig) -> list[str]:
    invariants: list[str] = []
    snippets: list[str] = []
    module_lower = module.lower()

    max_chars = 250_000
    used = 0
    for f in files:
        if f.suffix.lower() not in {".py", ".ts", ".tsx", ".js", ".jsx", ".yaml", ".yml", ".json"}:
            continue
        file_lower = f.as_posix().lower()
        if "/tests/" in file_lower or "/test_" in file_lower or file_lower.endswith("_test.py") or "/conftest.py" in file_lower:
            continue
        try:
            text = f.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        lower = text.lower()
        snippets.append(lower)
        used += len(lower)
        if used >= max_chars:
            break

    corpus = "\n".join(snippets)
    if not corpus:
        return invariants

    seen: set[str] = set()
    for inv_def in config.invariants:
        try:
            hits = len(re.findall(inv_def.pattern, corpus, re.IGNORECASE))
        except re.error:
            continue
        if hits < inv_def.min_hits:
            continue

        if inv_def.roles:
            role_ok = any(r in role for r in inv_def.roles)
            # module_contains acts as OR fallback for role filter
            module_ok = bool(inv_def.module_contains) and inv_def.module_contains in module_lower
            if not role_ok and not module_ok:
                continue
        elif inv_def.module_contains and inv_def.module_contains not in module_lower:
            continue

        if inv_def.not_module_contains and inv_def.not_module_contains in module_lower:
            continue

        entry = f"{module}>{inv_def.name}: {inv_def.description}"
        if entry not in seen:
            seen.add(entry)
            invariants.append(entry)

    return invariants


def structural_scan(root: Path, config: KbgenConfig) -> ScanData:
    ignore_dirs = IGNORED_DIR_NAMES | config.ignore_dirs
    ignored_modules = {k for k, v in config.module_overrides.items() if v.get("ignore")}
    all_entry_names = ENTRYPOINT_NAMES | config.entry_points
    module_roots = resolve_module_roots(
        root,
        module_strategy=config.module_strategy,
        configured_module_roots=config.module_roots,
    )

    files = collect_source_files(root, extra_ignore=ignore_dirs)
    modules: dict[str, list[Path]] = defaultdict(list)
    for f in files:
        mod = module_for_path(f, root, module_roots=module_roots)
        if mod not in ignored_modules:
            modules[mod].append(f)

    exports: dict[str, list[str]] = defaultdict(list)
    anchors: dict[str, list[str]] = defaultdict(list)
    module_names = set(modules.keys())
    deps: dict[str, set[str]] = {m: set() for m in module_names}
    entry_modules: set[str] = set()
    file_deps: set[tuple[str, str]] = set()
    route_entries: set[str] = set()
    auth_markers: set[str] = set()

    pkg_to_module: dict[str, str] = {}
    for setup_file in root.rglob("setup.py"):
        if any(part in ignore_dirs for part in setup_file.parts):
            continue
        try:
            setup_text = setup_file.read_text(encoding="utf-8", errors="ignore")
            for m in re.finditer(r"name\s*=\s*['\"]([^'\"]+)['\"]", setup_text):
                pkg_name = m.group(1).replace("-", "_")
                mod = module_for_path(setup_file, root, module_roots=module_roots)
                pkg_to_module[pkg_name] = mod
                break
        except Exception:
            pass
    for toml_file in root.rglob("pyproject.toml"):
        if any(part in ignore_dirs for part in toml_file.parts):
            continue
        try:
            toml_text = toml_file.read_text(encoding="utf-8", errors="ignore")
            for m in re.finditer(r'^name\s*=\s*["\']([^"\']+)["\']', toml_text, flags=re.MULTILINE):
                pkg_name = m.group(1).replace("-", "_")
                mod = module_for_path(toml_file, root, module_roots=module_roots)
                pkg_to_module[pkg_name] = mod
                break
        except Exception:
            pass
    for mod_name, mod_files in modules.items():
        for f in mod_files:
            if f.name == "__init__.py":
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

            if file.name in all_entry_names:
                entry_modules.add(module)

            for candidate in parse_import_candidates(text, file.suffix.lower()):
                resolved_module, resolved_path = resolve_import_target(
                    candidate,
                    file,
                    root,
                    module_names,
                    pkg_to_module,
                    module_roots=module_roots,
                )
                if resolved_module and resolved_module != module:
                    deps[module].add(resolved_module)
                if resolved_path is not None:
                    dst_rel = resolved_path.relative_to(root).as_posix()
                    if dst_rel != src_rel:
                        file_deps.add((src_rel, dst_rel))
        exports[module] = sorted(exp_names)[:12]
        anchors[module] = sorted(exp_anchors)

    return ScanData(
        root=root,
        modules=dict(modules),
        deps=deps,
        entry_modules=entry_modules,
        exports=exports,
        anchors=anchors,
        file_deps=sorted(file_deps),
        route_index=sorted(route_entries)[:ROUTE_INDEX_LIMIT],
        auth_chain=sorted(auth_markers)[:16],
    )


def synthesize(scan: ScanData, config: KbgenConfig) -> dict[str, ModuleSynthesis]:
    used_by: dict[str, set[str]] = {m: set() for m in scan.modules}
    for src, targets in scan.deps.items():
        for target in targets:
            used_by[target].add(src)

    out: dict[str, ModuleSynthesis] = {}
    for module, files in scan.modules.items():
        role = infer_role(module, files, module in scan.entry_modules, config)
        summary = infer_module_summary(module, role, files, config)
        deps = sorted(scan.deps.get(module, set()))
        inv = infer_module_invariants(module, role, files, config)

        # apply module overrides from kbgen.json
        override = config.module_overrides.get(module, {})
        if "role" in override:
            role = [override["role"]]
        if "summary" in override:
            summary = override["summary"]

        if module in scan.entry_modules:
            inv.append(f"{module}>entry")
        if "test" in role:
            inv.append(f"{module}>no_prod_path")

        out[module] = ModuleSynthesis(
            role=role,
            summary=summary,
            exports=scan.exports.get(module, []),
            anchors=scan.anchors.get(module, []),
            deps=deps,
            used_by=sorted(used_by.get(module, set())),
            invariants=sorted(dict.fromkeys(inv))[:12],
        )
    return out
