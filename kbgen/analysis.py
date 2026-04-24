from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

from kbgen.constants import ENTRYPOINT_NAMES, IGNORED_DIR_NAMES, ROUTE_INDEX_LIMIT
from kbgen.import_resolver import resolve_import_target
from kbgen.parsing import (
    collect_source_files,
    extract_export_anchors,
    extract_exports,
    module_for_path,
    parse_import_candidates,
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
    if is_python and any(k in joined for k in ("controller", "blueprint", "views", "view")):
        if "routing" not in tags:
            tags.append("routing")
    if is_python and any(k in joined for k in ("task", "worker", "celery", "consumer")):
        tags.append("worker")
    if any(k in module.lower() for k in ("auth", "login", "session")):
        tags.append("auth")
    if any(k in module.lower() for k in ("db", "repo", "model", "store")):
        tags.append("data")
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


def infer_module_summary(module: str, role: list[str], files: list[Path]) -> str:
    lower = module.lower()
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


def infer_module_invariants(module: str, role: list[str], files: list[Path]) -> list[str]:
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

    uuid_hits = len(re.findall(r"uuid\s*[_-]?v?7|uuidv7|install_uuid_v7|uuid7", corpus))
    utc_hits = len(re.findall(r"timezone\.utc|\butc\b|utcnow", corpus))
    datetime_hits = len(re.findall(r"datetime|timestamp|expires|expiration|created_at|updated_at", corpus))
    auth_hits = len(re.findall(r"jwt|bearer|keycloak|openid|oidc|oauth|access token|refresh token", corpus))
    dlq_hits = len(re.findall(r"\bdlq\b|dead[_ -]?letter", corpus))
    replay_hits = len(re.findall(r"replay|discard|bulk-replay|metrics", corpus))
    schema_hits = len(re.findall(r"schema|model|alembic|migration|dao|repository", corpus))
    delegation_hits = len(re.findall(r"is_auth_service|auth_consumer_handler|authconsumerhandlerfactory", corpus))
    http_error_hits = len(re.findall(r"unauthorizedexception|forbiddenexception|www-authenticate|www_authenticate", corpus))
    no_api_key_hits = len(re.findall(r"jwt-only auth|api key fields removed|api key auth superseded", corpus))
    s2s_hits = len(re.findall(r"allowed_callers|caller_azp|caller_restriction", corpus))
    pii_crypto_hits = len(
        re.findall(
            r"fernet|encrypt_data_bulk|encrypted_fields|filename_hash|email_hash|blind index|reencrypt|pii",
            corpus,
        )
    )

    if uuid_hits >= 1:
        invariants.append(
            f"{module}>uuid_v7: primary identifiers are expected to use UUIDv7 generation/migration paths"
        )

    if utc_hits >= 2 and datetime_hits >= 2:
        invariants.append(
            f"{module}>utc_datetime: use timezone-aware UTC datetimes and avoid naive local timestamps"
        )

    auth_module = ("auth" in module_lower) or ("auth" in role)
    is_service_like = any(r in role for r in ("routing", "auth", "service", "data", "worker")) or "auth" in module_lower or "common" in module_lower
    if auth_module or (is_service_like and auth_hits >= 5):
        invariants.append(
            f"{module}>jwt_or_oidc_auth: protected paths are expected to validate JWT/OIDC bearer identity"
        )

    is_auth_service_module = "auth" in module_lower and "service" in module_lower
    if delegation_hits >= 1 and not is_auth_service_module:
        invariants.append(
            f"{module}>auth_check_delegation: non-auth services delegate all authorization to"
            f" auth-service via POST /api/v1/auth/check; never re-validate JWT locally"
        )

    if http_error_hits >= 2:
        invariants.append(
            f"{module}>http_error_contract: 401 means unauthenticated (missing/invalid token, includes"
            f" WWW-Authenticate header); 403 means authenticated but forbidden; never swap them"
        )

    if dlq_hits >= 2 and replay_hits >= 1:
        invariants.append(
            f"{module}>dlq_replay_flow: dead-letter entries support controlled replay/discard recovery flows"
        )

    is_service_module = module_lower.endswith("-service") or module_lower.endswith("_service")
    explicit_no_api_key_owner = is_service_module or auth_module or ("common" in module_lower)
    no_api_key_expected = (no_api_key_hits >= 1 and explicit_no_api_key_owner) or (
        is_service_module and (auth_module or delegation_hits >= 1 or http_error_hits >= 1)
    )
    if no_api_key_expected:
        invariants.append(
            f"{module}>no_api_key: authentication is JWT/OIDC only (DMS-146);"
            f" API key support has been removed and must not be re-introduced"
        )

    if s2s_hits >= 1:
        invariants.append(
            f"{module}>s2s_azp_restriction: service-to-service endpoints restrict callers by azp JWT"
            f" claim via @allowed_callers; every permitted caller must be explicitly allowlisted"
        )

    pii_owner = ("auth" in module_lower) or ("record" in module_lower) or ("common" in module_lower)
    if pii_owner and pii_crypto_hits >= 3:
        invariants.append(
            f"{module}>pii_fernet_encryption: PII fields (for example user name/email and file"
            f" filename) are stored encrypted with Fernet key chains and blind-index lookups"
        )

    if "data" in role and schema_hits >= 3:
        invariants.append(
            f"{module}>schema_boundary: persistence should flow through schema/model/dao boundaries"
        )

    return invariants


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


def synthesize(scan: ScanData) -> dict[str, ModuleSynthesis]:
    used_by: dict[str, set[str]] = {m: set() for m in scan.modules}
    for src, targets in scan.deps.items():
        for target in targets:
            used_by[target].add(src)

    out: dict[str, ModuleSynthesis] = {}
    for module, files in scan.modules.items():
        role = infer_role(module, files, module in scan.entry_modules)
        deps = sorted(scan.deps.get(module, set()))
        inv = infer_module_invariants(module, role, files)
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
            invariants=sorted(dict.fromkeys(inv))[:12],
        )
    return out
