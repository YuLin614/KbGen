from __future__ import annotations

from pathlib import Path

from kbgen.parsing import module_for_path


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

    if candidate.startswith("@/") or candidate.startswith("~/"):
        resolved = resolve_absolute_like_import(root, candidate[2:])
        if resolved is not None:
            module = module_for_path(resolved, root)
            return (module if module in module_names else None), resolved

    if candidate.startswith("src/"):
        resolved = resolve_absolute_like_import(root, candidate)
        if resolved is not None:
            module = module_for_path(resolved, root)
            return (module if module in module_names else None), resolved

    head = candidate.split("/")[0].split(".")[0]
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


def resolve_relative_import(file: Path, target: str, root: Path) -> Path | None:
    base = (file.parent / target).resolve()
    candidates: list[Path] = [base]

    for ext in (".py", ".ts", ".tsx", ".js", ".jsx"):
        candidates.append(base.with_suffix(ext))

    for ext in (".py", ".ts", ".tsx", ".js", ".jsx"):
        candidates.append(base / ("index" + ext))

    for c in candidates:
        if c.exists() and c.is_file() and root in c.parents:
            return c
    return None


def resolve_relative_python_import(file: Path, target: str, root: Path) -> Path | None:
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
