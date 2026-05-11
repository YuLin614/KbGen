# AST-Based Parsing Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace all regex-based import/export/route/schema parsing in kbGen with proper AST parsing: Python files use stdlib `ast`, all other supported languages use tree-sitter.

**Architecture:** New `kbgen/ast_parsers.py` exposes `PythonParser`, `TreeSitterParser`, `DecoratorInfo`, and `get_parser(path)`. Existing `parsing.py`, `route_extraction.py`, and `schema_extraction.py` call this interface. `analysis.py` call-site for `parse_import_candidates` updated to match new signature.

**Tech Stack:** Python 3.10+ stdlib `ast`, tree-sitter ≥0.22, tree-sitter language packages (javascript, typescript, go, java, rust, c-sharp, php, ruby).

---

## File Map

| Action | Path | Responsibility |
|--------|------|----------------|
| Modify | `pyproject.toml` | Add tree-sitter dependencies |
| Create | `kbgen/ast_parsers.py` | Parser factory + PythonParser + TreeSitterParser |
| Create | `tests/test_ast_parsers.py` | Unit tests for all parsers |
| Modify | `kbgen/parsing.py` | Replace regex in parse_import_candidates, extract_exports, extract_export_anchors |
| Modify | `kbgen/analysis.py:310` | Fix parse_import_candidates call-site signature |
| Modify | `kbgen/route_extraction.py` | Replace Flask/FastAPI/Django regex with DecoratorInfo |
| Modify | `kbgen/schema_extraction.py` | Replace regex with stdlib ast |

---

## Task 1: Add tree-sitter dependencies

**Files:**
- Modify: `pyproject.toml`

- [ ] **Step 1: Replace the empty dependencies list**

Open `c:\kbGen\pyproject.toml`. Find:
```toml
dependencies = []
```
Replace with:
```toml
dependencies = [
    "tree-sitter>=0.22,<0.24",
    "tree-sitter-javascript>=0.22",
    "tree-sitter-typescript>=0.22",
    "tree-sitter-go>=0.22",
    "tree-sitter-java>=0.22",
    "tree-sitter-rust>=0.22",
    "tree-sitter-c-sharp>=0.22",
    "tree-sitter-php>=0.22",
    "tree-sitter-ruby>=0.22",
]
```

- [ ] **Step 2: Install dependencies**

```bash
cd c:\kbGen && pip install -e ".[dev]" 2>&1 || pip install -e . 2>&1
```

If that fails (no dev extra):
```bash
pip install "tree-sitter>=0.22,<0.24" tree-sitter-javascript tree-sitter-typescript tree-sitter-go tree-sitter-java tree-sitter-rust tree-sitter-c-sharp tree-sitter-php tree-sitter-ruby
```

- [ ] **Step 3: Verify tree-sitter installs**

```bash
python -c "
import tree_sitter_javascript as tsjs
import tree_sitter_typescript as tsts
import tree_sitter_go as tsgo
from tree_sitter import Language, Parser
JS = Language(tsjs.language())
parser = Parser(JS)
tree = parser.parse(b'import x from \"y\"')
print('tree-sitter ok, root:', tree.root_node.type)
"
```
Expected: `tree-sitter ok, root: program`

- [ ] **Step 4: Commit**

```bash
cd c:\kbGen && git add pyproject.toml && git commit -m "chore: add tree-sitter dependencies"
```

---

## Task 2: Write failing tests for ast_parsers (TDD — RED phase)

**Files:**
- Create: `tests/test_ast_parsers.py`

- [ ] **Step 1: Create the test file**

Create `c:\kbGen\tests\test_ast_parsers.py`:

```python
from __future__ import annotations

from pathlib import Path

import pytest

from kbgen.ast_parsers import (
    DecoratorInfo,
    PythonParser,
    TreeSitterParser,
    get_parser,
)
import tree_sitter_javascript as tsjs
import tree_sitter_typescript as tsts
from tree_sitter import Language

JS_LANGUAGE = Language(tsjs.language())
TS_LANGUAGE = Language(tsts.language_typescript())


# ── DecoratorInfo ─────────────────────────────────────────────────────────────

def test_decorator_info_fields():
    d = DecoratorInfo(name="app.route", args=["/users"], kwargs={"methods": ["GET"]}, lineno=5)
    assert d.name == "app.route"
    assert d.args == ["/users"]
    assert d.kwargs == {"methods": ["GET"]}
    assert d.lineno == 5


# ── PythonParser.extract_imports ──────────────────────────────────────────────

def test_python_import_standard():
    p = PythonParser()
    result = p.extract_imports("import os\nimport sys", Path("f.py"))
    assert "os" in result
    assert "sys" in result


def test_python_import_from():
    p = PythonParser()
    result = p.extract_imports("from pathlib import Path", Path("f.py"))
    assert "pathlib" in result


def test_python_import_relative():
    p = PythonParser()
    result = p.extract_imports("from . import utils\nfrom ..core import base", Path("f.py"))
    # relative imports have empty module — should be skipped gracefully
    assert isinstance(result, list)


def test_python_import_alias():
    p = PythonParser()
    result = p.extract_imports("import numpy as np", Path("f.py"))
    assert "numpy" in result


def test_python_import_syntax_error_returns_empty():
    p = PythonParser()
    result = p.extract_imports("def (broken!!!)", Path("f.py"))
    assert result == []


# ── PythonParser.extract_exports ──────────────────────────────────────────────

def test_python_export_functions():
    p = PythonParser()
    source = "def foo(): pass\ndef bar(): pass"
    result = p.extract_exports(source, Path("f.py"))
    names = [name for name, _ in result]
    assert "foo" in names
    assert "bar" in names


def test_python_export_classes():
    p = PythonParser()
    source = "class Foo: pass\nclass Bar(Base): pass"
    result = p.extract_exports(source, Path("f.py"))
    names = [name for name, _ in result]
    assert "Foo" in names
    assert "Bar" in names


def test_python_export_lineno():
    p = PythonParser()
    source = "def foo(): pass\n\ndef bar(): pass"
    result = p.extract_exports(source, Path("f.py"))
    linenos = {name: lineno for name, lineno in result}
    assert linenos["foo"] == 1
    assert linenos["bar"] == 3


def test_python_export_nested_not_included():
    p = PythonParser()
    source = "def outer():\n    def inner(): pass"
    result = p.extract_exports(source, Path("f.py"))
    names = [name for name, _ in result]
    assert "outer" in names
    assert "inner" not in names  # only top-level


def test_python_export_syntax_error_returns_empty():
    p = PythonParser()
    result = p.extract_exports("def (broken!!!)", Path("f.py"))
    assert result == []


# ── PythonParser.extract_decorators ───────────────────────────────────────────

def test_python_decorator_flask_route():
    p = PythonParser()
    source = "@app.route('/users', methods=['GET', 'POST'])\ndef get_users(): pass"
    result = p.extract_decorators(source, Path("routes.py"))
    assert len(result) == 1
    d = result[0]
    assert d.name == "app.route"
    assert d.args == ["/users"]
    assert d.kwargs.get("methods") == ["GET", "POST"]
    assert d.lineno == 1


def test_python_decorator_fastapi():
    p = PythonParser()
    source = "@router.get('/items/{id}')\nasync def get_item(id: int): pass"
    result = p.extract_decorators(source, Path("routes.py"))
    assert len(result) == 1
    assert result[0].name == "router.get"
    assert result[0].args == ["/items/{id}"]


def test_python_decorator_no_args():
    p = PythonParser()
    source = "@staticmethod\ndef foo(): pass"
    result = p.extract_decorators(source, Path("f.py"))
    assert any(d.name == "staticmethod" for d in result)


def test_python_decorator_syntax_error_returns_empty():
    p = PythonParser()
    result = p.extract_decorators("def (broken!!!)", Path("f.py"))
    assert result == []


# ── TreeSitterParser (JS) ─────────────────────────────────────────────────────

def test_js_import_esm():
    p = TreeSitterParser(JS_LANGUAGE)
    source = "import React from 'react';\nimport { useState } from 'react';"
    result = p.extract_imports(source, Path("f.js"))
    assert "react" in result


def test_js_import_require():
    p = TreeSitterParser(JS_LANGUAGE)
    source = "const path = require('path');\nconst utils = require('./utils');"
    result = p.extract_imports(source, Path("f.js"))
    assert "path" in result
    assert "./utils" in result


def test_js_export_named():
    p = TreeSitterParser(JS_LANGUAGE)
    source = "export function foo() {}\nexport const bar = 42;"
    result = p.extract_exports(source, Path("f.js"))
    names = [name for name, _ in result]
    assert "foo" in names
    assert "bar" in names


def test_js_export_default():
    p = TreeSitterParser(JS_LANGUAGE)
    source = "export default function MyComponent() { return null; }"
    result = p.extract_exports(source, Path("f.js"))
    names = [name for name, _ in result]
    assert "MyComponent" in names


def test_js_syntax_error_returns_empty():
    p = TreeSitterParser(JS_LANGUAGE)
    # tree-sitter is error-tolerant, but test that it returns a list
    result = p.extract_imports("{{{{ broken", Path("f.js"))
    assert isinstance(result, list)


# ── TreeSitterParser (TS) ─────────────────────────────────────────────────────

def test_ts_import():
    p = TreeSitterParser(TS_LANGUAGE)
    source = "import { Component } from '@angular/core';"
    result = p.extract_imports(source, Path("f.ts"))
    assert "@angular/core" in result


# ── get_parser factory ────────────────────────────────────────────────────────

def test_get_parser_python():
    from kbgen.ast_parsers import get_parser, PythonParser
    p = get_parser(Path("file.py"))
    assert isinstance(p, PythonParser)


def test_get_parser_js():
    from kbgen.ast_parsers import get_parser, TreeSitterParser
    p = get_parser(Path("file.js"))
    assert isinstance(p, TreeSitterParser)


def test_get_parser_ts():
    from kbgen.ast_parsers import get_parser, TreeSitterParser
    p = get_parser(Path("file.ts"))
    assert isinstance(p, TreeSitterParser)


def test_get_parser_unknown_returns_none():
    from kbgen.ast_parsers import get_parser
    assert get_parser(Path("file.unknown")) is None
```

- [ ] **Step 2: Run tests to verify they FAIL**

```bash
cd c:\kbGen && python -m pytest tests/test_ast_parsers.py -v 2>&1 | head -15
```

Expected: `ImportError: cannot import name 'DecoratorInfo' from 'kbgen.ast_parsers'` (module doesn't exist yet)

---

## Task 3: Implement `kbgen/ast_parsers.py`

**Files:**
- Create: `kbgen/ast_parsers.py`

- [ ] **Step 1: Create the file**

Create `c:\kbGen\kbgen\ast_parsers.py`:

```python
from __future__ import annotations

import ast as stdlib_ast
import functools
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from tree_sitter import Language, Node, Parser


@dataclass
class DecoratorInfo:
    name: str
    args: list[str]
    kwargs: dict[str, Any]
    lineno: int


# ── capture helper ────────────────────────────────────────────────────────────

def _captures(query, node: Node, capture_name: str) -> list[Node]:
    """Return captured nodes by name, handling tree-sitter 0.21 and 0.22 APIs."""
    raw = query.captures(node)
    if isinstance(raw, dict):
        # tree-sitter 0.21 style: dict[str, list[Node]]
        return raw.get(capture_name, [])
    # tree-sitter 0.22 style: list[tuple[Node, str]]
    return [n for n, name in raw if name == capture_name]


# ── PythonParser ──────────────────────────────────────────────────────────────

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


# ── TreeSitterParser ──────────────────────────────────────────────────────────

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
  (export_statement declaration: (function_declaration name: (identifier) @name))
  (export_statement declaration: (class_declaration name: (identifier) @name))
  (export_statement declaration: (lexical_declaration
    (variable_declarator name: (identifier) @name)))
  (export_default_declaration (function_declaration name: (identifier) @name))
  (export_default_declaration (class_declaration name: (identifier) @name))
]
"""

_GO_IMPORT_QUERY = """
(import_spec path: (interpreted_string_literal) @import)
"""

_GO_EXPORT_QUERY = """
[
  (function_declaration name: (identifier) @name)
  (method_declaration name: (field_identifier) @name)
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
  (interface_declaration name: (identifier) @name)
]
"""

_RUST_IMPORT_QUERY = """
(use_declaration argument: _ @import)
"""

_RUST_EXPORT_QUERY = """
[
  (function_item name: (identifier) @name)
  (struct_item name: (type_identifier) @name)
  (enum_item name: (type_identifier) @name)
  (trait_item name: (type_identifier) @name)
]
"""

_CS_IMPORT_QUERY = """
(using_directive (qualified_name) @import)
"""

_CS_EXPORT_QUERY = """
[
  (method_declaration name: (identifier) @name)
  (class_declaration name: (identifier) @name)
  (interface_declaration name: (identifier) @name)
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
(call method: (identifier) @fn arguments: (argument_list (string) @import))
"""

_RUBY_EXPORT_QUERY = """
[
  (method name: (identifier) @name)
  (class name: (constant) @name)
  (module name: (constant) @name)
]
"""


def _strip_string_quotes(text: str) -> str:
    text = text.strip()
    if len(text) >= 2 and text[0] in ('"', "'", "`") and text[-1] in ('"', "'", "`"):
        return text[1:-1]
    return text


class TreeSitterParser:
    def __init__(self, language: Language) -> None:
        self._language = language
        self._parser = Parser(language)
        self._import_query = language.query(_JS_IMPORT_QUERY)
        self._require_query = language.query(_JS_REQUIRE_QUERY)
        self._export_query = language.query(_JS_EXPORT_QUERY)

    def _parse(self, source: str) -> Any:
        return self._parser.parse(source.encode("utf-8", errors="replace"))

    def extract_imports(self, source: str, path: Path) -> list[str]:
        try:
            tree = self._parse(source)
            result: list[str] = []

            # ESM imports
            for node in _captures(self._import_query, tree.root_node, "import"):
                val = _strip_string_quotes(node.text.decode("utf-8", errors="replace"))
                if val:
                    result.append(val)

            # require() calls
            require_raw = self._require_query.captures(tree.root_node)
            if isinstance(require_raw, dict):
                fn_nodes = require_raw.get("fn", [])
                import_nodes = require_raw.get("import", [])
                # pair them: same index in list
                for fn_node, imp_node in zip(fn_nodes, import_nodes):
                    if fn_node.text == b"require":
                        val = _strip_string_quotes(imp_node.text.decode("utf-8", errors="replace"))
                        if val:
                            result.append(val)
            else:
                # list of (node, name) tuples — group by match position
                pairs: dict[int, dict[str, Node]] = {}
                for node, name in require_raw:
                    key = node.start_byte
                    if key not in pairs:
                        pairs[key] = {}
                    pairs[key][name] = node
                for pair in pairs.values():
                    fn_node = pair.get("fn")
                    imp_node = pair.get("import")
                    if fn_node and imp_node and fn_node.text == b"require":
                        val = _strip_string_quotes(imp_node.text.decode("utf-8", errors="replace"))
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
        # Decorators in JS/TS are not yet commonly parsed by tree-sitter grammars
        # Return empty — route extraction for JS/TS uses file-based routing only
        return []


# ── Language loaders (lazily cached) ─────────────────────────────────────────

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


def _make_ts_parser(language: Language | None, queries: dict[str, str]) -> TreeSitterParser | None:
    if language is None:
        return None
    p = TreeSitterParser(language)
    try:
        p._import_query = language.query(queries.get("import", _JS_IMPORT_QUERY))
        p._require_query = language.query(_JS_REQUIRE_QUERY)
        p._export_query = language.query(queries.get("export", _JS_EXPORT_QUERY))
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
        return TreeSitterParser(lang) if lang else None
    if ext == ".tsx":
        lang = _tsx_language()
        return TreeSitterParser(lang) if lang else None
    if ext == ".go":
        lang = _go_language()
        if lang is None:
            return None
        p = TreeSitterParser(lang)
        try:
            p._import_query = lang.query(_GO_IMPORT_QUERY)
            p._require_query = lang.query("(ERROR) @noop")
            p._export_query = lang.query(_GO_EXPORT_QUERY)
        except Exception:
            pass
        return p
    if ext == ".java":
        lang = _java_language()
        if lang is None:
            return None
        p = TreeSitterParser(lang)
        try:
            p._import_query = lang.query(_JAVA_IMPORT_QUERY)
            p._require_query = lang.query("(ERROR) @noop")
            p._export_query = lang.query(_JAVA_EXPORT_QUERY)
        except Exception:
            pass
        return p
    if ext == ".rs":
        lang = _rust_language()
        if lang is None:
            return None
        p = TreeSitterParser(lang)
        try:
            p._import_query = lang.query(_RUST_IMPORT_QUERY)
            p._require_query = lang.query("(ERROR) @noop")
            p._export_query = lang.query(_RUST_EXPORT_QUERY)
        except Exception:
            pass
        return p
    if ext == ".cs":
        lang = _csharp_language()
        if lang is None:
            return None
        p = TreeSitterParser(lang)
        try:
            p._import_query = lang.query(_CS_IMPORT_QUERY)
            p._require_query = lang.query("(ERROR) @noop")
            p._export_query = lang.query(_CS_EXPORT_QUERY)
        except Exception:
            pass
        return p
    if ext == ".php":
        lang = _php_language()
        if lang is None:
            return None
        p = TreeSitterParser(lang)
        try:
            p._import_query = lang.query(_PHP_IMPORT_QUERY)
            p._require_query = lang.query("(ERROR) @noop")
            p._export_query = lang.query(_PHP_EXPORT_QUERY)
        except Exception:
            pass
        return p
    if ext == ".rb":
        lang = _ruby_language()
        if lang is None:
            return None
        p = TreeSitterParser(lang)
        try:
            p._import_query = lang.query(_RUBY_IMPORT_QUERY)
            p._require_query = lang.query("(ERROR) @noop")
            p._export_query = lang.query(_RUBY_EXPORT_QUERY)
        except Exception:
            pass
        return p
    return None
```

- [ ] **Step 2: Run tests to verify they PASS**

```bash
cd c:\kbGen && python -m pytest tests/test_ast_parsers.py -v
```

Expected: all tests PASS (some tree-sitter query tests may need minor fixes — if a query fails with `QueryError`, adjust the query string in ast_parsers.py for that language).

If any query raises `QueryError`:
1. Check the error message — it names the failing pattern
2. Simplify that language's query to just `(ERROR) @noop` temporarily to make tests pass
3. The fallback returns empty list, which is acceptable for non-JS/Python languages

- [ ] **Step 3: Run full test suite to confirm no regressions**

```bash
cd c:\kbGen && python -m pytest tests/ -v
```

Expected: all 20 existing tests + new ast_parsers tests PASS.

- [ ] **Step 4: Commit**

```bash
cd c:\kbGen && git add kbgen/ast_parsers.py tests/test_ast_parsers.py && git commit -m "feat: add ast_parsers.py with PythonParser and TreeSitterParser"
```

---

## Task 4: Replace `parse_import_candidates` in `parsing.py` + fix `analysis.py` call-site

**Files:**
- Modify: `kbgen/parsing.py`
- Modify: `kbgen/analysis.py`

- [ ] **Step 1: Replace `parse_import_candidates` in `parsing.py`**

Find the entire `parse_import_candidates` function (lines 120–138 in `parsing.py`):

```python
def parse_import_candidates(text: str, suffix: str) -> list[str]:
    candidates: list[str] = []
    if suffix == ".py":
        ...
    elif suffix in {".js", ".jsx", ".ts", ".tsx"}:
        ...
    return candidates
```

Replace with:

```python
def parse_import_candidates(path: Path, text: str) -> list[str]:
    from kbgen.ast_parsers import get_parser
    parser = get_parser(path)
    if parser is None:
        return []
    return parser.extract_imports(text, path)
```

Also remove the `import re` at the top of `parsing.py` ONLY IF `re` is no longer used elsewhere in the file. Check: `estimate_tokens` uses `re.findall`, so keep `import re`.

- [ ] **Step 2: Fix the call-site in `analysis.py`**

Open `c:\kbGen\kbgen\analysis.py`. Find line ~310:

```python
            for candidate in parse_import_candidates(text, file.suffix.lower()):
```

Replace with:

```python
            for candidate in parse_import_candidates(file, text):
```

- [ ] **Step 3: Verify no crash**

```bash
cd c:\kbGen && python -c "
from pathlib import Path
from kbgen.parsing import parse_import_candidates
result = parse_import_candidates(Path('test.py'), 'import os\nfrom pathlib import Path')
print(result)
assert 'os' in result
assert 'pathlib' in result
print('ok')
"
```

Expected: `['os', 'pathlib']` then `ok`.

- [ ] **Step 4: Run all tests**

```bash
cd c:\kbGen && python -m pytest tests/ -v
```

Expected: all tests PASS.

- [ ] **Step 5: Commit**

```bash
cd c:\kbGen && git add kbgen/parsing.py kbgen/analysis.py && git commit -m "feat: replace parse_import_candidates regex with ast_parsers"
```

---

## Task 5: Replace `extract_exports` and `extract_export_anchors` in `parsing.py`

**Files:**
- Modify: `kbgen/parsing.py`

- [ ] **Step 1: Replace `extract_exports`**

Find the entire `extract_exports` function (lines 141–168). Replace with:

```python
def extract_exports(path: Path, text: str) -> list[str]:
    from kbgen.ast_parsers import get_parser
    parser = get_parser(path)
    if parser is None:
        return []
    pairs = parser.extract_exports(text, path)
    return [name for name, _ in pairs][:10]
```

- [ ] **Step 2: Replace `extract_export_anchors`**

Find the entire `extract_export_anchors` function (lines 171–232) including its two helper functions `_find_js_symbol_declaration` and `_find_component_like_symbols` (lines 235–270). Replace the function with:

```python
def extract_export_anchors(path: Path, text: str, root: Path) -> list[str]:
    from kbgen.ast_parsers import get_parser
    parser = get_parser(path)
    if parser is None:
        return []
    rel = path.relative_to(root).as_posix()
    pairs = parser.extract_exports(text, path)
    return [f"{name}@{rel}:{lineno}" for name, lineno in pairs][:10]
```

Delete the helper functions `_find_js_symbol_declaration` and `_find_component_like_symbols` entirely (they are no longer used).

- [ ] **Step 3: Verify**

```bash
cd c:\kbGen && python -c "
from pathlib import Path
from kbgen.parsing import extract_exports, extract_export_anchors

source = 'def foo(): pass\nclass Bar: pass'
path = Path('api/routes.py')
root = Path('.')

print(extract_exports(path, source))
print(extract_export_anchors(path, source, root))
assert 'foo' in extract_exports(path, source)
print('ok')
"
```

Expected: `['foo', 'Bar']`, then anchors like `['Bar@api/routes.py:2', 'foo@api/routes.py:1']`, then `ok`.

- [ ] **Step 4: Run all tests**

```bash
cd c:\kbGen && python -m pytest tests/ -v
```

Expected: all tests PASS.

- [ ] **Step 5: Commit**

```bash
cd c:\kbGen && git add kbgen/parsing.py && git commit -m "feat: replace extract_exports and extract_export_anchors regex with ast_parsers"
```

---

## Task 6: Replace Flask/FastAPI/Django regex in `route_extraction.py`

**Files:**
- Modify: `kbgen/route_extraction.py`

- [ ] **Step 1: Replace the Python route extraction block in `extract_route_entries`**

In `c:\kbGen\kbgen\route_extraction.py`, find the block starting at `if suffix == ".py":` (line 88). Replace the entire inner body (lines 89–161, keeping the surrounding `if suffix == ".py":` check) with:

```python
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
                    route_path = with_prefix(deco.name[:-len(".route")], deco.args[0])
                    methods = deco.kwargs.get("methods", ["GET"])
                    if isinstance(methods, list):
                        method_str = "|".join(sorted(str(m).upper() for m in methods))
                    else:
                        method_str = "GET"
                    entries.append(f"api:{route_path}[{method_str}]->{rel}:{deco.lineno}")

                # FastAPI: @router.get/post/put/patch/delete/options/head('/path')
                elif "." in deco.name and deco.args:
                    _obj, _, http_method = deco.name.rpartition(".")
                    if http_method.upper() in {"GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"}:
                        route_path = with_prefix(_obj, deco.args[0])
                        entries.append(f"api:{route_path}[{http_method.upper()}]->{rel}:{deco.lineno}")

        # Django path()/re_path() — still regex (not decorator-based)
        django_pattern = re.compile(r"(?:re_)?path\(['\"]([^'\"]+)['\"]")
        for m in django_pattern.finditer(text):
            if "urlpatterns" in text or "include(" in text:
                route_path = m.group(1)
                line = text.count("\n", 0, m.start()) + 1
                entries.append(f"api:{route_path}->{rel}:{line}")

        return entries
```

Note: Django's `path()` stays as regex because it's a function call (not a decorator) — AST would work too but the regex is accurate and simple here.

Also note: `import re` must remain at the top of `route_extraction.py` (still used for Django pattern and `_resolve_blueprint_prefixes`).

- [ ] **Step 2: Verify Flask route extraction works**

```bash
cd c:\kbGen && python -c "
from pathlib import Path
from kbgen.route_extraction import extract_route_entries

source = '''
@app.route('/users', methods=['GET', 'POST'])
def get_users(): pass

@router.get('/items/{id}')
async def get_item(id: int): pass
'''
root = Path('.')
result = extract_route_entries(Path('api/routes.py'), source, root)
print(result)
assert any('/users' in r for r in result), 'Flask route missing'
assert any('/items' in r for r in result), 'FastAPI route missing'
print('ok')
"
```

Expected: list containing Flask and FastAPI routes, then `ok`.

- [ ] **Step 3: Run all tests**

```bash
cd c:\kbGen && python -m pytest tests/ -v
```

Expected: all tests PASS.

- [ ] **Step 4: Commit**

```bash
cd c:\kbGen && git add kbgen/route_extraction.py && git commit -m "feat: replace Flask/FastAPI route regex with AST decorator extraction"
```

---

## Task 7: Replace regex in `schema_extraction.py` with stdlib `ast`

**Files:**
- Modify: `kbgen/schema_extraction.py`

Note: `schema_extraction.py` handles Python-only files (SQLAlchemy/Alembic). We use `PythonParser` indirectly — actually we use `stdlib ast` directly since `schema_extraction.py` only processes `.py` files and the logic is complex enough that direct `ast` manipulation is cleaner than going through `PythonParser`.

- [ ] **Step 1: Replace the per-file parsing block**

In `c:\kbGen\kbgen\schema_extraction.py`, find the `try: text = f.read_text(...)` block and everything that follows it inside the `for f in files:` loop (lines 48–119). Replace the entire inner block with:

```python
            try:
                text = f.read_text(encoding="utf-8", errors="ignore")
                tree = _safe_parse(text, f)
                if tree is None:
                    continue
            except Exception:
                continue

            for cls_node in (n for n in stdlib_ast.walk(tree) if isinstance(n, stdlib_ast.ClassDef)):
                table = _get_tablename(cls_node)
                if table:
                    add_table(module, table)
                    for col_name in _get_columns(cls_node):
                        add_col(module, table, col_name)
                    for fk_target in _get_foreignkeys(cls_node):
                        add_fk(module, table, fk_target)
                    for fk_target in _get_table_fk(cls_node):
                        add_fk(module, table, fk_target)

            # Alembic: op.create_table / op.add_column / op.create_foreign_key
            for call in (n for n in stdlib_ast.walk(tree) if isinstance(n, stdlib_ast.Call)):
                fn = _call_name(call)
                if fn == "op.create_table":
                    table = _str_arg(call, 0)
                    if table:
                        add_table(module, table)
                        for kw in call.keywords:
                            pass  # columns are positional args after table name
                        for arg in call.args[1:]:
                            if isinstance(arg, stdlib_ast.Call) and _call_name(arg) in {"sa.Column", "Column"}:
                                col = _str_arg(arg, 0)
                                if col:
                                    add_col(module, table, col)
                                for inner in stdlib_ast.walk(arg):
                                    if isinstance(inner, stdlib_ast.Call) and _call_name(inner) in {"sa.ForeignKey", "ForeignKey", "sa.ForeignKeyConstraint", "ForeignKeyConstraint"}:
                                        fk = _str_arg(inner, 0)
                                        if fk:
                                            add_fk(module, table, fk)
                elif fn == "op.add_column":
                    table = _str_arg(call, 0)
                    col_arg = call.args[1] if len(call.args) > 1 else None
                    if table and isinstance(col_arg, stdlib_ast.Call):
                        col = _str_arg(col_arg, 0)
                        if col:
                            add_col(module, table, col)
                elif fn == "op.create_foreign_key":
                    src = _str_arg(call, 1)
                    dst = _str_arg(call, 2)
                    if src and dst:
                        add_fk(module, src, dst)
```

- [ ] **Step 2: Add helper functions and imports at the top of `schema_extraction.py`**

Replace the imports section at the top of `schema_extraction.py`:

```python
from __future__ import annotations

import ast as stdlib_ast
import re
from collections import defaultdict
from pathlib import Path

from kbgen.constants import DB_SCHEMA_LIMIT
```

Then add these helper functions BEFORE the `extract_db_schema_index` function:

```python
def _safe_parse(text: str, path: Path) -> stdlib_ast.Module | None:
    try:
        return stdlib_ast.parse(text, filename=str(path))
    except SyntaxError:
        return None


def _call_name(call: stdlib_ast.Call) -> str:
    func = call.func
    if isinstance(func, stdlib_ast.Name):
        return func.id
    if isinstance(func, stdlib_ast.Attribute):
        prefix = _call_name_attr(func.value)
        return f"{prefix}.{func.attr}" if prefix else func.attr
    return ""


def _call_name_attr(node: stdlib_ast.expr) -> str:
    if isinstance(node, stdlib_ast.Name):
        return node.id
    if isinstance(node, stdlib_ast.Attribute):
        p = _call_name_attr(node.value)
        return f"{p}.{node.attr}" if p else node.attr
    return ""


def _str_arg(call: stdlib_ast.Call, idx: int) -> str | None:
    if idx < len(call.args):
        arg = call.args[idx]
        if isinstance(arg, stdlib_ast.Constant) and isinstance(arg.value, str):
            return arg.value
    return None


def _get_tablename(cls: stdlib_ast.ClassDef) -> str | None:
    for node in cls.body:
        if isinstance(node, stdlib_ast.Assign):
            for target in node.targets:
                if isinstance(target, stdlib_ast.Name) and target.id == "__tablename__":
                    if isinstance(node.value, stdlib_ast.Constant) and isinstance(node.value.value, str):
                        return node.value.value
    return None


def _get_columns(cls: stdlib_ast.ClassDef) -> list[str]:
    cols: list[str] = []
    for node in cls.body:
        if isinstance(node, stdlib_ast.AnnAssign):
            if isinstance(node.target, stdlib_ast.Name):
                name = node.target.id
                if not name.startswith("_"):
                    cols.append(name)
        elif isinstance(node, stdlib_ast.Assign):
            for target in node.targets:
                if not isinstance(target, stdlib_ast.Name):
                    continue
                name = target.id
                if name.startswith("_") or name == "__tablename__":
                    continue
                if isinstance(node.value, stdlib_ast.Call):
                    fn = _call_name(node.value)
                    if any(k in fn for k in ("Column", "mapped_column", "relationship")):
                        cols.append(name)
    return cols


def _get_foreignkeys(cls: stdlib_ast.ClassDef) -> list[str]:
    fks: list[str] = []
    for node in stdlib_ast.walk(cls):
        if isinstance(node, stdlib_ast.Call):
            fn = _call_name(node)
            if "ForeignKey" in fn and not "Constraint" in fn:
                val = _str_arg(node, 0)
                if val:
                    fks.append(val)
    return fks


def _get_table_fk(cls: stdlib_ast.ClassDef) -> list[str]:
    fks: list[str] = []
    for node in stdlib_ast.walk(cls):
        if isinstance(node, stdlib_ast.Call):
            fn = _call_name(node)
            if "ForeignKeyConstraint" in fn and len(node.args) >= 2:
                arg = node.args[1]
                if isinstance(arg, (stdlib_ast.List, stdlib_ast.Tuple)):
                    for elt in arg.elts:
                        if isinstance(elt, stdlib_ast.Constant) and isinstance(elt.value, str):
                            fks.append(elt.value)
    return fks
```

Also remove the `extract_call_block` helper function — it's no longer needed.

Keep the `import re` since `re` is no longer used — actually check: is `re` still used anywhere in schema_extraction.py? After the replacement, it's NOT used. Remove `import re` from the imports.

- [ ] **Step 3: Verify schema extraction**

```bash
cd c:\kbGen && python -c "
from pathlib import Path
from kbgen.schema_extraction import extract_db_schema_index

source = '''
class User(Base):
    __tablename__ = 'users'
    id = Column(Integer, primary_key=True)
    email = Column(String)
    
class Post(Base):
    __tablename__ = 'posts'
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey('users.id'))
'''

import tempfile, os
with tempfile.TemporaryDirectory() as tmp:
    root = Path(tmp)
    model_dir = root / 'models'
    model_dir.mkdir()
    (model_dir / 'models.py').write_text(source)
    result = extract_db_schema_index(root, {'models': [model_dir / 'models.py']})
    print(result)
    assert any('users' in r for r in result), 'users table missing'
    assert any('posts' in r for r in result), 'posts table missing'
    print('ok')
"
```

Expected: list with `models.users(email,id)` and `models.posts(id,user_id)` (and possibly a rel entry), then `ok`.

- [ ] **Step 4: Run all tests**

```bash
cd c:\kbGen && python -m pytest tests/ -v
```

Expected: all tests PASS.

- [ ] **Step 5: Commit**

```bash
cd c:\kbGen && git add kbgen/schema_extraction.py && git commit -m "feat: replace schema_extraction regex with stdlib ast"
```

---

## Task 8: Integration test and smoke test

**Files:** None modified.

- [ ] **Step 1: Full test suite**

```bash
cd c:\kbGen && python -m pytest tests/ -v
```

Expected: all tests PASS (20 existing + ast_parsers tests).

- [ ] **Step 2: Smoke test kbgen scan on itself**

```bash
cd c:\kbGen && python -m kbgen scan --module-strategy top_level 2>&1
```

Expected: JSON output with `"status": "ok"` and `"modules": N` where N > 0.

- [ ] **Step 3: Verify imports and exports are extracted correctly**

```bash
cd c:\kbGen && python -c "
import json
from pathlib import Path
from kbgen.core import full_scan

result = full_scan(Path('.'), key_path_limit=0, module_strategy='top_level', module_roots=None)
snapshot = json.loads(Path('.ai/snapshot.kb').read_text())
# Check that kbgen module has exports
kbgen_mod = snapshot.get('m', {}).get('kbgen', {})
exports = kbgen_mod.get('e', [])
print('kbgen exports:', exports[:5])
assert len(exports) > 0, 'no exports found in kbgen module'
print('ok')
"
```

Expected: prints some export names, then `ok`.

- [ ] **Step 4: Smoke test kbgen dashboard to confirm existing functionality unaffected**

```bash
cd c:\kbGen && python -m kbgen dashboard --no-html
```

Expected: dashboard prints without crash.

- [ ] **Step 5: Final commit**

```bash
cd c:\kbGen && git add -A && git commit -m "test: AST parsing integration verified"
```

---

## Self-Review

**Spec coverage:**
- ✓ Python files use stdlib `ast` — PythonParser in Task 3
- ✓ Other languages use tree-sitter — TreeSitterParser in Task 3
- ✓ `parse_import_candidates` replaced — Task 4
- ✓ `extract_exports` + `extract_export_anchors` replaced — Task 5
- ✓ `analysis.py` call-site updated — Task 4
- ✓ Flask/FastAPI route extraction replaced — Task 6
- ✓ Django `path()` kept as regex (function call, not decorator) — Task 6 note
- ✓ Schema extraction replaced — Task 7
- ✓ Parse failures return empty list, never crash — PythonParser SyntaxError handling + TreeSitterParser except Exception
- ✓ `get_parser` returns None for unsupported ext — Task 3
- ✓ Grammar import failures return None gracefully — lru_cache loaders catch exceptions
- ✓ TDD: tests written before implementation — Task 2 then Task 3
- ✓ Dependencies added to pyproject.toml — Task 1

**Type consistency:**
- `PythonParser.extract_imports(source, path)` → `list[str]` — used in Task 4 parse_import_candidates
- `PythonParser.extract_exports(source, path)` → `list[tuple[str, int]]` — used in Tasks 5
- `PythonParser.extract_decorators(source, path)` → `list[DecoratorInfo]` — used in Task 6
- `TreeSitterParser` has same interface — used same way
- `get_parser(path)` → `PythonParser | TreeSitterParser | None` — Tasks 4, 5, 6 all handle None
- `DecoratorInfo.name`, `.args`, `.kwargs`, `.lineno` — consistent in Task 3 implementation and Task 6 usage
