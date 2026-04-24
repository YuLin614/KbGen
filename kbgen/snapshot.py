from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path
from typing import Any

from kbgen.analysis import ModuleSynthesis, ScanData
from kbgen.constants import DEFAULT_KEY_PATH_LIMIT, ENTRYPOINT_NAMES, SOFT_TOKEN_MAX, SOFT_TOKEN_TARGET
from kbgen.parsing import estimate_tokens
from kbgen.schema_extraction import extract_db_schema_index


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

    if module == "components":
        business = [anchor for anchor in ranked if not is_ui_primitive_anchor(anchor)]
        if len(business) >= 1:
            return business

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
        if path_to_best_anchor and path in path_to_best_anchor:
            return path_to_best_anchor[path]
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
            for kw in ("controller", "blueprint", "views", "/api/", "router"):
                if kw in path:
                    points += 6
            for kw in ("health", "ping", "flask-base", "flask_base"):
                if kw in path:
                    points -= 10
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
            for kw in ("worker", "task", "celery", "run_worker", "consumer", "make_celery"):
                if kw in text:
                    points -= 8
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


def build_snapshot(
    synth: dict[str, ModuleSynthesis],
    scan: ScanData,
    key_path_limit: int = DEFAULT_KEY_PATH_LIMIT,
) -> dict[str, Any]:
    modules: dict[str, Any] = {}
    edges: list[list[str]] = []
    neg: list[str] = []
    path_to_best_anchor: dict[str, str] = {}

    for name, item in sorted(synth.items()):
        key_paths = module_key_paths(scan.root, scan.modules.get(name, []), item.exports, key_path_limit)
        ranked_anchors = rank_module_anchors(name, item.anchors)
        for anchor in ranked_anchors:
            if "@" not in anchor:
                continue
            sym, rest = anchor.split("@", 1)
            anchor_path = rest.rsplit(":", 1)[0] if ":" in rest else rest
            if anchor_path not in path_to_best_anchor:
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
        "ri": scan.route_index,
        "db": extract_db_schema_index(scan.root, scan.modules),
        "ac": scan.auth_chain[:12],
        "no": sorted(set(neg))[:30],
        "h": hints,
        "hf": file_hints,
        "hr": hint_rationales,
        "ls": ls,
    }
    return snapshot


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

    db = candidate.get("db", [])
    if isinstance(db, list) and len(db) > 0:
        keep_db = max(120, int(len(db) * scale))
        candidate["db"] = db[:keep_db]

    if token_size(candidate) > SOFT_TOKEN_MAX:
        for mod in modules.values():
            paths = mod.get("p", [])
            if isinstance(paths, list):
                mod["p"] = paths[:12]
        fd = candidate.get("fd", [])
        if isinstance(fd, list):
            candidate["fd"] = fd[:48]
        db = candidate.get("db", [])
        if isinstance(db, list):
            candidate["db"] = db[:160]

    return candidate
