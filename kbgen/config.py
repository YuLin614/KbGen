from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class InvariantDef:
    name: str
    pattern: str
    description: str
    min_hits: int = 1
    # if set, require at least one role match OR module_contains match
    roles: list[str] = field(default_factory=list)
    # if set (and roles empty), module name must contain this string
    module_contains: str = ""
    # if set, skip if module name contains this string
    not_module_contains: str = ""


@dataclass
class KbgenConfig:
    profile: str = "generic"
    entry_points: set[str] = field(default_factory=set)
    ignore_dirs: set[str] = field(default_factory=set)
    module_strategy: str = "auto"
    module_roots: list[str] = field(default_factory=list)
    # module name (or dir segment) → role name
    role_map: dict[str, str] = field(default_factory=dict)
    # module name → summary override
    summaries: dict[str, str] = field(default_factory=dict)
    invariants: list[InvariantDef] = field(default_factory=list)
    # module name → {role, summary, ignore}
    module_overrides: dict[str, dict] = field(default_factory=dict)


def _profiles_dir() -> Path:
    return Path(__file__).parent / "profiles"


def _load_profile(name: str) -> dict:
    path = _profiles_dir() / f"{name}.json"
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _detect_profile(root: Path) -> str:
    if (root / "manage.py").exists():
        return "django"

    for cfg in ("next.config.js", "next.config.ts", "next.config.mjs"):
        if (root / cfg).exists():
            return "nextjs"

    pkg_path = root / "package.json"
    if pkg_path.exists():
        try:
            pkg = json.loads(pkg_path.read_text(encoding="utf-8", errors="ignore"))
            all_deps = {**pkg.get("dependencies", {}), **pkg.get("devDependencies", {})}
            if "next" in all_deps:
                return "nextjs"
            if "express" in all_deps:
                return "express"
            if "fastify" in all_deps:
                return "fastify"
        except Exception:
            pass

    for req_path in (root / "requirements.txt", root / "requirements" / "base.txt"):
        if req_path.exists():
            try:
                text = req_path.read_text(encoding="utf-8", errors="ignore").lower()
                if "fastapi" in text:
                    return "fastapi"
                if "flask" in text:
                    return "flask"
                if "django" in text:
                    return "django"
            except Exception:
                pass

    pyproject_path = root / "pyproject.toml"
    if pyproject_path.exists():
        try:
            text = pyproject_path.read_text(encoding="utf-8", errors="ignore").lower()
            if "fastapi" in text:
                return "fastapi"
            if "flask" in text:
                return "flask"
            if "django" in text:
                return "django"
        except Exception:
            pass

    return "generic"


def _invariants_from_data(data: list[dict]) -> list[InvariantDef]:
    return [
        InvariantDef(
            name=inv["name"],
            pattern=inv["pattern"],
            description=inv["description"],
            min_hits=inv.get("min_hits", 1),
            roles=inv.get("roles", []),
            module_contains=inv.get("module_contains", ""),
            not_module_contains=inv.get("not_module_contains", ""),
        )
        for inv in data
    ]


def _apply_profile_data(cfg: KbgenConfig, data: dict) -> None:
    cfg.entry_points.update(data.get("entry_points", []))
    cfg.ignore_dirs.update(data.get("ignore_dirs", []))
    cfg.role_map.update(data.get("role_map", {}))
    cfg.summaries.update(data.get("summaries", {}))
    cfg.invariants.extend(_invariants_from_data(data.get("invariants", [])))


def load_config(root: Path) -> KbgenConfig:
    user_data: dict = {}
    user_config_path = root / "kbgen.json"
    if user_config_path.exists():
        try:
            user_data = json.loads(user_config_path.read_text(encoding="utf-8", errors="ignore"))
        except Exception:
            pass

    profile_name = user_data.get("profile", "auto")
    if profile_name == "auto":
        profile_name = _detect_profile(root)

    cfg = KbgenConfig(profile=profile_name)
    _apply_profile_data(cfg, _load_profile("generic"))

    if profile_name != "generic":
        _apply_profile_data(cfg, _load_profile(profile_name))

    # user kbgen.json overrides applied last
    cfg.entry_points.update(user_data.get("entry_points", []))
    cfg.ignore_dirs.update(user_data.get("ignore_dirs", []))
    cfg.module_strategy = user_data.get("module_strategy", "auto")
    cfg.module_roots = [str(item) for item in user_data.get("module_roots", [])]
    cfg.role_map.update(user_data.get("role_map", {}))
    cfg.summaries.update(user_data.get("summaries", {}))
    cfg.invariants.extend(_invariants_from_data(user_data.get("invariants", [])))
    cfg.module_overrides = user_data.get("modules", {})

    return cfg
