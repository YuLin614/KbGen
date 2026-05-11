from __future__ import annotations

import ast as stdlib_ast
from collections import defaultdict
from pathlib import Path

from kbgen.constants import DB_SCHEMA_LIMIT


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
        prefix = _attr_chain(func.value)
        return f"{prefix}.{func.attr}" if prefix else func.attr
    return ""


def _attr_chain(node: stdlib_ast.expr) -> str:
    if isinstance(node, stdlib_ast.Name):
        return node.id
    if isinstance(node, stdlib_ast.Attribute):
        p = _attr_chain(node.value)
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
            if "ForeignKey" in fn and "Constraint" not in fn:
                val = _str_arg(node, 0)
                if val:
                    fks.append(val)
    return fks


def _get_table_fk_constraints(cls: stdlib_ast.ClassDef) -> list[str]:
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


def extract_db_schema_index(root: Path, modules: dict[str, list[Path]]) -> list[str]:
    table_cols: dict[tuple[str, str], set[str]] = defaultdict(set)
    fk_edges: set[tuple[str, str, str]] = set()

    def add_table(module: str, table: str) -> None:
        if table:
            table_cols.setdefault((module, table), set())

    def add_col(module: str, table: str, col: str) -> None:
        if table and col:
            table_cols[(module, table)].add(col)

    def add_fk(module: str, src_table: str, target: str) -> None:
        target_table = target.split(".")[0] if "." in target else target
        if src_table and target_table:
            fk_edges.add((module, src_table, target_table))

    for module, files in modules.items():
        for f in files:
            if f.suffix.lower() != ".py":
                continue
            rel = f.relative_to(root).as_posix().lower()
            if "/tests/" in rel or "/test_" in rel or rel.endswith("_test.py"):
                continue
            if not any(k in rel for k in ("model", "alembic", "migration", "schema", "dao", "datastore")):
                continue
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
                    for fk_target in _get_table_fk_constraints(cls_node):
                        add_fk(module, table, fk_target)

            # Alembic migrations
            for call in (n for n in stdlib_ast.walk(tree) if isinstance(n, stdlib_ast.Call)):
                fn = _call_name(call)
                if fn == "op.create_table":
                    table = _str_arg(call, 0)
                    if table:
                        add_table(module, table)
                        for arg in call.args[1:]:
                            if isinstance(arg, stdlib_ast.Call):
                                col_fn = _call_name(arg)
                                if any(k in col_fn for k in ("Column", "sa.Column")):
                                    col = _str_arg(arg, 0)
                                    if col:
                                        add_col(module, table, col)
                                    for inner in stdlib_ast.walk(arg):
                                        if isinstance(inner, stdlib_ast.Call):
                                            inner_fn = _call_name(inner)
                                            if any(k in inner_fn for k in ("ForeignKey", "ForeignKeyConstraint")):
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

    module_tables: dict[str, set[str]] = defaultdict(set)
    for mod, table in table_cols.keys():
        module_tables[mod].add(table)
    for (module, table), cols in table_cols.items():
        known = module_tables.get(module, set())
        for col in cols:
            if not col.endswith("_id") or col == "id":
                continue
            stem = col[: -len("_id")]
            candidates = {stem, f"{stem}s", f"{stem}es"}
            if stem.endswith("y") and len(stem) > 1:
                candidates.add(f"{stem[:-1]}ies")
            target = next((c for c in candidates if c in known and c != table), None)
            if target:
                fk_edges.add((module, table, target))

    entries: list[str] = []
    for (module, table), cols in sorted(table_cols.items()):
        col_list = sorted(cols)
        if col_list:
            rendered = ",".join(col_list)
            entries.append(f"{module}.{table}({rendered})")
        else:
            entries.append(f"{module}.{table}")
    for module, src, dst in sorted(fk_edges):
        entries.append(f"rel:{module}.{src}->{dst}")

    return entries[:DB_SCHEMA_LIMIT]
