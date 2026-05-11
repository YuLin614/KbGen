from __future__ import annotations

import re
from pathlib import Path

from kbgen.constants import BLUEPRINT_PREFIX_CACHE


def _join_route(prefix: str, route_path: str) -> str:
    p = (prefix or "").strip()
    r = (route_path or "").strip()
    if not p:
        return r or "/"
    if not r:
        return p or "/"
    left = p[:-1] if p.endswith("/") else p
    right = r if r.startswith("/") else ("/" + r)
    out = left + right
    return out or "/"


def _resolve_blueprint_prefixes(blueprints_text: str) -> dict[str, str]:
    own_prefix: dict[str, str] = {}
    parent_edge: dict[str, tuple[str, str]] = {}

    bp_decl = re.compile(
        r"^\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*=\s*Blueprint\([^\n]*?url_prefix\s*=\s*['\"]([^'\"]*)['\"]",
        flags=re.MULTILINE,
    )
    register_decl = re.compile(
        r"^\s*([a-zA-Z_][a-zA-Z0-9_]*)\.register_blueprint\(\s*([a-zA-Z_][a-zA-Z0-9_]*)(?:\s*,\s*url_prefix\s*=\s*['\"]([^'\"]*)['\"])?",
        flags=re.MULTILINE,
    )

    for m in bp_decl.finditer(blueprints_text):
        own_prefix[m.group(1)] = m.group(2)

    for m in register_decl.finditer(blueprints_text):
        parent = m.group(1)
        child = m.group(2)
        extra_prefix = m.group(3) or ""
        if child not in parent_edge:
            parent_edge[child] = (parent, extra_prefix)

    memo: dict[str, str] = {}

    def full_prefix(name: str, visiting: set[str]) -> str:
        if name in memo:
            return memo[name]
        if name in visiting:
            return own_prefix.get(name, "")
        visiting.add(name)
        base = own_prefix.get(name, "")
        parent_info = parent_edge.get(name)
        if parent_info is None:
            memo[name] = base
            visiting.remove(name)
            return memo[name]
        parent_name, extra = parent_info
        parent_full = full_prefix(parent_name, visiting)
        combined = _join_route(_join_route(parent_full, extra), base)
        memo[name] = combined
        visiting.remove(name)
        return combined

    all_names = set(own_prefix) | set(parent_edge)
    return {name: full_prefix(name, set()) for name in all_names}


def _nearest_blueprints_file(path: Path, root: Path) -> Path | None:
    for parent in [path.parent, *path.parents]:
        if root not in parent.parents and parent != root:
            continue
        candidate = parent / "blueprints.py"
        if candidate.exists() and candidate.is_file() and root in candidate.parents:
            return candidate
    return None


def extract_route_entries(path: Path, text: str, root: Path) -> list[str]:
    suffix = path.suffix.lower()
    rel = path.relative_to(root).as_posix()
    lower_rel = rel.lower()

    if "/tests/" in lower_rel or "/test_" in lower_rel or lower_rel.startswith("tests/"):
        return []

    if suffix == ".py":
        from kbgen.ast_parsers import get_parser
        entries: list[str] = []
        bp_prefixes: dict[str, str] = {}
        local_bp_prefixes: dict[str, str] = {}

        blueprints_file = _nearest_blueprints_file(path, root)
        if blueprints_file is not None:
            cache_key = str(blueprints_file)
            if cache_key not in BLUEPRINT_PREFIX_CACHE:
                try:
                    bp_text = blueprints_file.read_text(encoding="utf-8", errors="ignore")
                    BLUEPRINT_PREFIX_CACHE[cache_key] = _resolve_blueprint_prefixes(bp_text)
                except Exception:
                    BLUEPRINT_PREFIX_CACHE[cache_key] = {}
            bp_prefixes = BLUEPRINT_PREFIX_CACHE.get(cache_key, {})

        try:
            local_bp_prefixes = _resolve_blueprint_prefixes(text)
        except Exception:
            local_bp_prefixes = {}

        def with_prefix(decorator_obj: str, route_path: str) -> str:
            obj = decorator_obj.split(".")[-1].strip()
            prefix = bp_prefixes.get(obj, "") or local_bp_prefixes.get(obj, "")
            return _join_route(prefix, route_path)

        parser = get_parser(path)
        if parser is not None:
            for deco in parser.extract_decorators(text, path):
                # Flask: @app.route('/path', methods=[...])
                if deco.name.endswith(".route") and deco.args:
                    route_path = with_prefix(deco.name[: -len(".route")], deco.args[0])
                    methods = deco.kwargs.get("methods", ["GET"])
                    if isinstance(methods, list):
                        method_str = "|".join(sorted(str(m).upper() for m in methods))
                    else:
                        method_str = "GET"
                    entries.append(f"api:{route_path}[{method_str}]->{rel}:{deco.lineno}")

                # FastAPI: @router.get/post/put/patch/delete('/path')
                elif "." in deco.name and deco.args:
                    _obj, _, http_method = deco.name.rpartition(".")
                    if http_method.upper() in {"GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"}:
                        route_path = with_prefix(_obj, deco.args[0])
                        entries.append(f"api:{route_path}[{http_method.upper()}]->{rel}:{deco.lineno}")

        # Django path()/re_path() — function calls, not decorators; keep regex
        django_pattern = re.compile(r"(?:re_)?path\(['\"]([^'\"]+)['\"]")
        for m in django_pattern.finditer(text):
            if "urlpatterns" in text or "include(" in text:
                route_path = m.group(1)
                line = text.count("\n", 0, m.start()) + 1
                entries.append(f"api:{route_path}->{rel}:{line}")

        if re.search(r"request\.args\.get\(['\"]lock_level['\"]", text):
            extra: list[str] = []
            seen = set(entries)
            for e in entries:
                m2 = re.match(r"api:([^\[]+)\[([^\]]+)\]->(.+)", e)
                if not m2:
                    continue
                route_path = m2.group(1)
                methods = {x.strip().upper() for x in m2.group(2).split("|") if x.strip()}
                tail = m2.group(3)
                if "GET" not in methods:
                    continue
                if not route_path.endswith("/files"):
                    continue
                variant = f"api:{route_path}?lock_level=[GET]->{tail}"
                if variant not in seen:
                    seen.add(variant)
                    extra.append(variant)
            entries.extend(extra)
        return entries

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
