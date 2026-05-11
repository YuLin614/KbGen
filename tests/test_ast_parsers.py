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
    assert "inner" not in names


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


def test_js_syntax_error_returns_list():
    p = TreeSitterParser(JS_LANGUAGE)
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
    p = get_parser(Path("file.py"))
    assert isinstance(p, PythonParser)


def test_get_parser_js():
    p = get_parser(Path("file.js"))
    assert isinstance(p, TreeSitterParser)


def test_get_parser_ts():
    p = get_parser(Path("file.ts"))
    assert isinstance(p, TreeSitterParser)


def test_get_parser_unknown_returns_none():
    assert get_parser(Path("file.unknown")) is None
