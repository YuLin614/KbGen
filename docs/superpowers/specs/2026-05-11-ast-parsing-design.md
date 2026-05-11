# AST-Based Parsing — Design Spec
Date: 2026-05-11

## Problem

kbGen's parsing is entirely regex-based. This causes:
- Aliased imports missed (`from x import y as z`)
- Dynamic imports missed (`import(...)`, computed requires)
- Complex decorator args missed (`@router.get("/users/{id}", tags=["users"])`)
- ORM inheritance and mixin patterns missed in schema extraction
- Ongoing regex maintenance burden for every edge case per language

## Goal

Replace all regex parsing in `parsing.py`, `route_extraction.py`, and `schema_extraction.py` with proper AST parsing. Python files use stdlib `ast` (official CPython parser, zero extra dep). All other languages use tree-sitter grammars (best available option in the Python ecosystem per language).

No regex fallback. Parse failures return empty results gracefully.

## Architecture

New module `kbgen/ast_parsers.py` acts as parser factory. Existing modules call its interface instead of running regex.

```
ast_parsers.py          ← NEW: factory + two parser implementations
  PythonParser          ← stdlib ast, handles .py files
  TreeSitterParser      ← tree-sitter, handles .js/.ts/.go/.java/.rs/.cs/.php/.rb

parsing.py              ← MODIFIED: import/export/anchor extraction
route_extraction.py     ← MODIFIED: decorator route extraction
schema_extraction.py    ← MODIFIED: ORM schema extraction (Python only)
pyproject.toml          ← MODIFIED: add tree-sitter dependencies
tests/test_ast_parsers.py ← NEW: parser unit tests
```

## `ast_parsers.py`

### Shared Interface

```python
@dataclass
class DecoratorInfo:
    name: str        # e.g. "app.route", "router.get"
    args: list[str]  # positional string args e.g. ["/users"]
    kwargs: dict     # keyword args e.g. {"methods": ["GET", "POST"]}
    lineno: int

class FileParser(Protocol):
    def extract_imports(self, source: str, path: Path) -> list[str]: ...
    def extract_exports(self, source: str, path: Path) -> list[tuple[str, int]]: ...
    def extract_decorators(self, source: str, path: Path) -> list[DecoratorInfo]: ...
```

### PythonParser (stdlib `ast`)

- `extract_imports`: walks `ast.Import` and `ast.ImportFrom` nodes → module names
- `extract_exports`: walks top-level `ast.FunctionDef`, `ast.AsyncFunctionDef`, `ast.ClassDef`, and `ast.Assign` (for `__all__`) → (name, lineno) pairs
- `extract_decorators`: walks `ast.FunctionDef.decorator_list` → `DecoratorInfo` with args and kwargs parsed from `ast.Call` nodes
- Failure: `except SyntaxError` → return `[]`

### TreeSitterParser

One instance per language, language object lazily initialized at module level.

**Import queries by language:**
```
# JS/TS
(import_statement source: (string) @import)
(call_expression function: (identifier) @fn (#eq? @fn "require")
  arguments: (arguments (string) @import))

# Go
(import_declaration (import_spec path: (interpreted_string_literal) @import))

# Java
(import_declaration (scoped_identifier) @import)

# Rust
(use_declaration argument: _ @import)
```

**Export queries by language:**
```
# JS/TS
(export_statement declaration: _ @export)
(export_default_declaration _ @export)

# Go: exported = capitalized top-level identifiers
(function_declaration name: (identifier) @name)
(type_declaration (type_spec name: (type_identifier) @name))
```

**Decorator query (JS/TS only):**
```
(decorator (call_expression
  function: _ @name
  arguments: (arguments) @args))
```

- Failure: `except Exception` → return `[]`

### Factory

```python
def get_parser(path: Path) -> FileParser | None:
    ext = path.suffix.lower()
    if ext == ".py":
        return PythonParser()
    if ext in {".js", ".jsx"}:
        return TreeSitterParser(_js_language())
    if ext in {".ts", ".tsx"}:
        return TreeSitterParser(_ts_language())
    if ext == ".go":
        return TreeSitterParser(_go_language())
    if ext == ".java":
        return TreeSitterParser(_java_language())
    if ext == ".rs":
        return TreeSitterParser(_rust_language())
    if ext == ".cs":
        return TreeSitterParser(_csharp_language())
    if ext == ".php":
        return TreeSitterParser(_php_language())
    if ext in {".rb"}:
        return TreeSitterParser(_ruby_language())
    return None
```

Language loaders (`_js_language()` etc.) are module-level cached via `functools.lru_cache`. Grammar import errors are caught and logged once; that language returns `None` from `get_parser` for the remainder of the session.

## Changes to `parsing.py`

### `parse_import_candidates(path, source)` → replaces regex version

```python
def parse_import_candidates(path: Path, source: str) -> list[str]:
    parser = get_parser(path)
    if parser is None:
        return []
    return parser.extract_imports(source, path)
```

Old regex implementation deleted.

### `extract_exports(path, source)` + `extract_export_anchors(path, source)` → merged

```python
def extract_exports(path: Path, source: str) -> list[str]:
    parser = get_parser(path)
    if parser is None:
        return []
    return [name for name, _ in parser.extract_exports(source, path)]

def extract_export_anchors(path: Path, source: str, root: Path) -> list[str]:
    parser = get_parser(path)
    if parser is None:
        return []
    rel = path.relative_to(root).as_posix()
    return [f"{name}@{rel}:{lineno}" for name, lineno in parser.extract_exports(source, path)]
```

Old regex implementations deleted. `analysis.py` callers of `extract_export_anchors` must also pass `root` — update call sites in `structural_scan()`.

## Changes to `route_extraction.py`

Current regex approach: match decorator strings with patterns like `@app\.route\(["'](.+?)["']`.

New approach: call `get_parser(path).extract_decorators(source, path)` → get `list[DecoratorInfo]` → apply existing framework-detection logic on structured data instead of raw strings.

```python
# Before (regex):
for m in re.finditer(r'@app\.route\(["\'](.+?)["\']', source):
    path_val = m.group(1)

# After (AST):
for deco in get_parser(path).extract_decorators(source, path):
    if deco.name in {"app.route", "bp.route"}:
        path_val = deco.args[0] if deco.args else None
        methods = deco.kwargs.get("methods", ["GET"])
```

Route output format unchanged: `api:/users[GET]->file.py:10`

Blueprint prefix resolution logic unchanged (it operates on already-extracted route data).

Next.js file-based routing unchanged (no AST needed — file path IS the route).

## Changes to `schema_extraction.py`

Currently regex-matches `__tablename__`, `Column(...)`, `ForeignKey(...)` in Python source text. Misses:
- Inherited columns from parent models
- Mixin-contributed columns
- Multi-line column definitions

New approach uses `PythonParser` directly (Python-only file, no tree-sitter needed):

```python
tree = ast.parse(source)
for node in ast.walk(tree):
    if isinstance(node, ast.ClassDef):
        tablename = _extract_tablename(node)    # ast.Assign __tablename__
        columns = _extract_columns(node)         # ast.Call Column(...) in assignments
        bases = [b.id for b in node.bases if isinstance(b, ast.Name)]
        # bases enables mixin tracking in a second pass
```

Output format unchanged: `users(id,email,created_at)->posts(id,user_id)`.

## Dependencies

Add to `pyproject.toml`:

```toml
[project]
dependencies = [
    "tree-sitter>=0.22",
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

No LLM/AI API dependency. All processing local.

## Error Handling

| Scenario | Behaviour |
|----------|-----------|
| tree-sitter grammar package not installed | `ImportError` caught at module init; `get_parser` returns `None` for that language; file contributes empty symbols |
| Source file has syntax errors | `SyntaxError` (Python) or tree-sitter partial parse; caught per-file; returns `[]` |
| tree-sitter query finds no matches | Returns `[]` — normal |
| `get_parser` returns `None` (unsupported ext) | Caller returns `[]` — same as current "no regex match" behaviour |

No exception propagates out of any parser method. Session never crashes due to one unparseable file.

## Testing

`tests/test_ast_parsers.py` covers:

```python
# Python imports — standard, from-import, alias
def test_python_imports_standard(): ...
def test_python_imports_from(): ...
def test_python_imports_alias(): ...

# Python exports — function, class, __all__
def test_python_exports_functions(): ...
def test_python_exports_classes(): ...

# Python decorators — Flask route with methods kwarg
def test_python_decorator_flask_route(): ...
def test_python_decorator_fastapi_get(): ...

# JS imports — ESM + CommonJS require
def test_js_imports_esm(): ...
def test_js_imports_require(): ...

# JS exports — named + default
def test_js_exports_named(): ...
def test_js_exports_default(): ...

# TS imports
def test_ts_imports(): ...

# Graceful failure
def test_syntax_error_python_returns_empty(): ...
def test_syntax_error_js_returns_empty(): ...
def test_unsupported_extension_returns_none(): ...

# Schema extraction
def test_schema_tablename(): ...
def test_schema_columns(): ...
def test_schema_foreign_key(): ...
```

Existing tests in `test_quality.py` and `test_report.py` must continue passing (no regression).

## Success Criteria

1. `kbgen scan` on a Python project extracts the same or more imports/exports than before
2. `kbgen scan` on a Next.js/TypeScript project correctly extracts ESM imports and exports
3. Flask/FastAPI routes with complex decorator args (path params, multiple methods) correctly extracted
4. SQLAlchemy models with mixin inheritance correctly extract column names
5. All new tests in `test_ast_parsers.py` pass
6. All 20 existing tests still pass
7. `kbgen scan` completes without crash on a project with syntax errors in some files
