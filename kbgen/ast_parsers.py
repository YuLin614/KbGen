from __future__ import annotations

import ast as stdlib_ast
import functools
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from tree_sitter import Language, Node, Parser, Query, QueryCursor


@dataclass
class DecoratorInfo:
    name: str
    args: list[str]
    kwargs: dict[str, Any]
    lineno: int


# ── capture helpers ───────────────────────────────────────────────────────────

def _captures(query: Query, node: Node, capture_name: str) -> list[Node]:
    """Return captured nodes by name using QueryCursor (tree-sitter 0.25.x)."""
    cursor = QueryCursor(query)
    raw = cursor.captures(node)
    # 0.25.x: dict[str, list[Node]]
    if isinstance(raw, dict):
        return raw.get(capture_name, [])
    # Older API fallback: list[tuple[Node, str]]
    return [n for n, name in raw if name == capture_name]


def _matches(query: Query, node: Node) -> list[tuple[int, dict[str, list[Node]]]]:
    """Return matches using QueryCursor (tree-sitter 0.25.x)."""
    cursor = QueryCursor(query)
    return cursor.matches(node)


# ── PythonParser helpers ──────────────────────────────────────────────────────

def _ast_name(node: stdlib_ast.expr) -> str:
    if isinstance(node, stdlib_ast.Name):
        return node.id
    if isinstance(node, stdlib_ast.Attribute):
        return f"{_ast_name(node.value)}.{node.attr}"
    return ""


def _parse_decorator(deco: stdlib_ast.expr) -> DecoratorInfo | None:
    if isinstance(deco, stdlib_ast.Call):
        name = _ast_name(deco.func)
        args: list[str] = []
        for a in deco.args:
            if isinstance(a, stdlib_ast.Constant) and isinstance(a.value, str):
                args.append(a.value)
        kwargs: dict[str, Any] = {}
        for kw in deco.keywords:
            if kw.arg == "methods":
                if isinstance(kw.value, (stdlib_ast.List, stdlib_ast.Tuple)):
                    methods = []
                    for elt in kw.value.elts:
                        if isinstance(elt, stdlib_ast.Constant):
                            methods.append(str(elt.value))
                    kwargs["methods"] = methods
            elif kw.arg and isinstance(kw.value, stdlib_ast.Constant):
                kwargs[kw.arg] = kw.value.value
        return DecoratorInfo(name=name, args=args, kwargs=kwargs, lineno=deco.lineno)
    elif isinstance(deco, (stdlib_ast.Name, stdlib_ast.Attribute)):
        name = _ast_name(deco)
        return DecoratorInfo(name=name, args=[], kwargs={}, lineno=deco.lineno)
    return None


# ── PythonParser ──────────────────────────────────────────────────────────────

class PythonParser:
    def extract_imports(self, source: str, path: Path) -> list[str]:
        try:
            tree = stdlib_ast.parse(source, filename=str(path))
        except SyntaxError:
            return []
        result: list[str] = []
        for node in stdlib_ast.walk(tree):
            if isinstance(node, stdlib_ast.Import):
                for alias in node.names:
                    result.append(alias.name)
            elif isinstance(node, stdlib_ast.ImportFrom):
                if node.module:
                    result.append(node.module)
        return result

    def extract_exports(self, source: str, path: Path) -> list[tuple[str, int]]:
        try:
            tree = stdlib_ast.parse(source, filename=str(path))
        except SyntaxError:
            return []
        result: list[tuple[str, int]] = []
        for node in tree.body:  # top-level only
            if isinstance(node, (stdlib_ast.FunctionDef, stdlib_ast.AsyncFunctionDef, stdlib_ast.ClassDef)):
                result.append((node.name, node.lineno))
            elif isinstance(node, stdlib_ast.Assign):
                for target in node.targets:
                    if isinstance(target, stdlib_ast.Name) and target.id == "__all__":
                        if isinstance(node.value, (stdlib_ast.List, stdlib_ast.Tuple)):
                            for elt in node.value.elts:
                                if isinstance(elt, stdlib_ast.Constant) and isinstance(elt.value, str):
                                    result.append((elt.value, node.lineno))
        return result[:10]

    def extract_decorators(self, source: str, path: Path) -> list[DecoratorInfo]:
        try:
            tree = stdlib_ast.parse(source, filename=str(path))
        except SyntaxError:
            return []
        result: list[DecoratorInfo] = []
        for node in stdlib_ast.walk(tree):
            if isinstance(node, (stdlib_ast.FunctionDef, stdlib_ast.AsyncFunctionDef)):
                for deco in node.decorator_list:
                    info = _parse_decorator(deco)
                    if info is not None:
                        result.append(info)
        return result


# ── Query strings ─────────────────────────────────────────────────────────────

_JS_IMPORT_QUERY = """
(import_statement source: (string) @import)
"""

_JS_REQUIRE_QUERY = """
(call_expression
  function: (identifier) @fn
  arguments: (arguments (string) @import))
"""

_JS_EXPORT_QUERY = """
[
  (export_statement (function_declaration name: (identifier) @name))
  (export_statement (class_declaration name: (identifier) @name))
  (export_statement declaration: (function_declaration name: (identifier) @name))
  (export_statement declaration: (class_declaration name: (identifier) @name))
  (export_statement declaration: (lexical_declaration
    (variable_declarator name: (identifier) @name)))
]
"""

# TypeScript uses type_identifier for class names
_TS_EXPORT_QUERY = """
[
  (export_statement (function_declaration name: (identifier) @name))
  (export_statement (class_declaration name: (type_identifier) @name))
  (export_statement declaration: (function_declaration name: (identifier) @name))
  (export_statement declaration: (class_declaration name: (type_identifier) @name))
  (export_statement declaration: (lexical_declaration
    (variable_declarator name: (identifier) @name)))
]
"""

_GO_IMPORT_QUERY = """
(import_spec path: (interpreted_string_literal) @import)
"""

_GO_EXPORT_QUERY = """
[
  (function_declaration name: (identifier) @name)
  (type_declaration (type_spec name: (type_identifier) @name))
]
"""

_JAVA_IMPORT_QUERY = """
(import_declaration (scoped_identifier) @import)
"""

_JAVA_EXPORT_QUERY = """
[
  (method_declaration name: (identifier) @name)
  (class_declaration name: (identifier) @name)
]
"""

_RUST_IMPORT_QUERY = """
(use_declaration [(scoped_identifier) (scoped_use_list) (identifier)] @import)
"""

_RUST_EXPORT_QUERY = """
[
  (function_item name: (identifier) @name)
  (struct_item name: (type_identifier) @name)
  (enum_item name: (type_identifier) @name)
]
"""

_CS_IMPORT_QUERY = """
(using_directive [(qualified_name) (identifier)] @import)
"""

_CS_EXPORT_QUERY = """
[
  (method_declaration name: (identifier) @name)
  (class_declaration name: (identifier) @name)
]
"""

_PHP_IMPORT_QUERY = """
(namespace_use_clause (qualified_name) @import)
"""

_PHP_EXPORT_QUERY = """
[
  (function_definition name: (name) @name)
  (class_declaration name: (name) @name)
]
"""

_RUBY_IMPORT_QUERY = """
(call (identifier) @fn (argument_list (string) @import))
"""

_RUBY_EXPORT_QUERY = """
[
  (method name: (identifier) @name)
  (class name: (constant) @name)
]
"""


def _strip_quotes(text: str) -> str:
    text = text.strip()
    if len(text) >= 2 and text[0] in ('"', "'", "`") and text[-1] in ('"', "'", "`"):
        return text[1:-1]
    return text


# ── TreeSitterParser ──────────────────────────────────────────────────────────

class TreeSitterParser:
    def __init__(self, language: Language) -> None:
        self._language = language
        self._parser = Parser(language)
        self._use_require_pairing = True  # JS-style: need to check fn name

        # Build import query — same for JS and TS
        self._import_query = Query(language, _JS_IMPORT_QUERY)
        self._require_query = Query(language, _JS_REQUIRE_QUERY)

        # Build export query — JS uses identifier, TS uses type_identifier for classes
        try:
            self._export_query = Query(language, _JS_EXPORT_QUERY)
        except Exception:
            try:
                self._export_query = Query(language, _TS_EXPORT_QUERY)
            except Exception:
                # Fallback: just capture named function declarations
                self._export_query = Query(language, "(function_declaration name: (identifier) @name)")

    def _parse(self, source: str):
        return self._parser.parse(source.encode("utf-8", errors="replace"))

    def extract_imports(self, source: str, path: Path) -> list[str]:
        try:
            tree = self._parse(source)
            result: list[str] = []

            # Primary import query (ESM imports / language-specific)
            for node in _captures(self._import_query, tree.root_node, "import"):
                val = _strip_quotes(node.text.decode("utf-8", errors="replace"))
                if val:
                    result.append(val)

            # Require query (JS) - use matches() for paired fn+import
            if self._use_require_pairing:
                for _pattern_idx, capture_dict in _matches(self._require_query, tree.root_node):
                    fn_nodes = capture_dict.get("fn", [])
                    imp_nodes = capture_dict.get("import", [])
                    if fn_nodes and imp_nodes:
                        fn_node = fn_nodes[0]
                        imp_node = imp_nodes[0]
                        if fn_node.text == b"require":
                            val = _strip_quotes(imp_node.text.decode("utf-8", errors="replace"))
                            if val:
                                result.append(val)
            else:
                # For non-JS languages with import-like queries that don't need pairing
                for node in _captures(self._require_query, tree.root_node, "import"):
                    val = _strip_quotes(node.text.decode("utf-8", errors="replace"))
                    if val:
                        result.append(val)

            return result
        except Exception:
            return []

    def extract_exports(self, source: str, path: Path) -> list[tuple[str, int]]:
        try:
            tree = self._parse(source)
            result: list[tuple[str, int]] = []
            seen: set[str] = set()
            for node in _captures(self._export_query, tree.root_node, "name"):
                name = node.text.decode("utf-8", errors="replace")
                if name and name not in seen:
                    seen.add(name)
                    result.append((name, node.start_point[0] + 1))
            return result[:10]
        except Exception:
            return []

    def extract_decorators(self, source: str, path: Path) -> list[DecoratorInfo]:
        return []  # JS/TS uses file-based routing; decorator extraction not needed


# ── Language loaders ──────────────────────────────────────────────────────────

@functools.lru_cache(maxsize=None)
def _js_language() -> Language | None:
    try:
        import tree_sitter_javascript as tsjs
        return Language(tsjs.language())
    except Exception:
        return None


@functools.lru_cache(maxsize=None)
def _ts_language() -> Language | None:
    try:
        import tree_sitter_typescript as tsts
        return Language(tsts.language_typescript())
    except Exception:
        return None


@functools.lru_cache(maxsize=None)
def _tsx_language() -> Language | None:
    try:
        import tree_sitter_typescript as tsts
        return Language(tsts.language_tsx())
    except Exception:
        return None


@functools.lru_cache(maxsize=None)
def _go_language() -> Language | None:
    try:
        import tree_sitter_go as tsgo
        return Language(tsgo.language())
    except Exception:
        return None


@functools.lru_cache(maxsize=None)
def _java_language() -> Language | None:
    try:
        import tree_sitter_java as tsjava
        return Language(tsjava.language())
    except Exception:
        return None


@functools.lru_cache(maxsize=None)
def _rust_language() -> Language | None:
    try:
        import tree_sitter_rust as tsrust
        return Language(tsrust.language())
    except Exception:
        return None


@functools.lru_cache(maxsize=None)
def _csharp_language() -> Language | None:
    try:
        import tree_sitter_c_sharp as tscs
        return Language(tscs.language())
    except Exception:
        return None


@functools.lru_cache(maxsize=None)
def _php_language() -> Language | None:
    try:
        import tree_sitter_php as tsphp
        return Language(tsphp.language_php())
    except Exception:
        return None


@functools.lru_cache(maxsize=None)
def _ruby_language() -> Language | None:
    try:
        import tree_sitter_ruby as tsruby
        return Language(tsruby.language())
    except Exception:
        return None


def _make_ts_parser_with_queries(
    language: Language, import_q: str, export_q: str, *, use_require_pairing: bool = False
) -> TreeSitterParser:
    """Create a TreeSitterParser with language-specific import/export queries."""
    p = TreeSitterParser(language)
    p._use_require_pairing = use_require_pairing
    try:
        p._import_query = Query(language, import_q)
        # For non-JS languages, use an empty/harmless secondary query
        # We'll just re-use the import query but won't run pairing logic
        p._require_query = Query(language, import_q)
        p._export_query = Query(language, export_q)
    except Exception:
        pass
    return p


def get_parser(path: Path) -> PythonParser | TreeSitterParser | None:
    ext = path.suffix.lower()
    if ext == ".py":
        return PythonParser()
    if ext in {".js", ".jsx"}:
        lang = _js_language()
        return TreeSitterParser(lang) if lang else None
    if ext == ".ts":
        lang = _ts_language()
        return _make_ts_parser_with_queries(lang, _JS_IMPORT_QUERY, _TS_EXPORT_QUERY, use_require_pairing=True) if lang else None
    if ext == ".tsx":
        lang = _tsx_language()
        return _make_ts_parser_with_queries(lang, _JS_IMPORT_QUERY, _TS_EXPORT_QUERY, use_require_pairing=True) if lang else None
    if ext == ".go":
        lang = _go_language()
        return _make_ts_parser_with_queries(lang, _GO_IMPORT_QUERY, _GO_EXPORT_QUERY) if lang else None
    if ext == ".java":
        lang = _java_language()
        return _make_ts_parser_with_queries(lang, _JAVA_IMPORT_QUERY, _JAVA_EXPORT_QUERY) if lang else None
    if ext == ".rs":
        lang = _rust_language()
        return _make_ts_parser_with_queries(lang, _RUST_IMPORT_QUERY, _RUST_EXPORT_QUERY) if lang else None
    if ext == ".cs":
        lang = _csharp_language()
        return _make_ts_parser_with_queries(lang, _CS_IMPORT_QUERY, _CS_EXPORT_QUERY) if lang else None
    if ext == ".php":
        lang = _php_language()
        return _make_ts_parser_with_queries(lang, _PHP_IMPORT_QUERY, _PHP_EXPORT_QUERY) if lang else None
    if ext == ".rb":
        lang = _ruby_language()
        return _make_ts_parser_with_queries(lang, _RUBY_IMPORT_QUERY, _RUBY_EXPORT_QUERY) if lang else None
    return None
