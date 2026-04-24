from __future__ import annotations

import re
from collections import defaultdict
from pathlib import Path

from kbgen.constants import DB_SCHEMA_LIMIT


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

    def extract_call_block(text: str, open_paren_idx: int) -> str:
        depth = 0
        for i in range(open_paren_idx, len(text)):
            ch = text[i]
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
                if depth == 0:
                    return text[open_paren_idx + 1 : i]
        return ""

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
            except Exception:
                continue

            for tab in re.finditer(r"__tablename__\s*=\s*['\"]([^'\"]+)['\"]", text):
                table = tab.group(1)
                add_table(module, table)

                class_start = text.rfind("\nclass ", 0, tab.start())
                if class_start < 0:
                    class_start = 0
                next_class = text.find("\nclass ", tab.end())
                body = text[class_start : next_class if next_class >= 0 else len(text)]

                for colm in re.finditer(
                    r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*(?::[^=\n]+)?=\s*(?:db\.)?(?:Column|mapped_column)\s*\(",
                    body,
                    re.MULTILINE,
                ):
                    col_name = colm.group(1)
                    if not col_name.startswith("_"):
                        add_col(module, table, col_name)
                for fkm in re.finditer(r"ForeignKey\(\s*['\"]([^'\"]+)['\"]", body):
                    add_fk(module, table, fkm.group(1))

            for tm in re.finditer(r"(?:db\.)?Table\s*\(\s*['\"]([^'\"]+)['\"]", text):
                table = tm.group(1)
                add_table(module, table)
                open_idx = text.find("(", tm.start())
                if open_idx < 0:
                    continue
                block = extract_call_block(text, open_idx)
                if not block:
                    continue
                for colm in re.finditer(r"(?:sa\.)?Column\s*\(\s*['\"]([^'\"]+)['\"]", block):
                    add_col(module, table, colm.group(1))
                for fkm in re.finditer(r"(?:sa\.)?ForeignKey\(\s*['\"]([^'\"]+)['\"]", block):
                    add_fk(module, table, fkm.group(1))
                for fkm in re.finditer(
                    r"(?:sa\.)?ForeignKeyConstraint\s*\(\s*\[[^\]]*\]\s*,\s*\[\s*['\"]([^'\"]+)['\"]",
                    block,
                ):
                    add_fk(module, table, fkm.group(1))

            for tm in re.finditer(r"op\.create_table\s*\(\s*['\"]([^'\"]+)['\"]", text):
                table = tm.group(1)
                add_table(module, table)
                open_idx = text.find("(", tm.start())
                if open_idx < 0:
                    continue
                block = extract_call_block(text, open_idx)
                if not block:
                    continue
                for colm in re.finditer(r"sa\.Column\s*\(\s*['\"]([^'\"]+)['\"]", block):
                    add_col(module, table, colm.group(1))
                for fkm in re.finditer(r"(?:sa\.)?ForeignKey\(\s*['\"]([^'\"]+)['\"]", block):
                    add_fk(module, table, fkm.group(1))
                for fkm in re.finditer(
                    r"sa\.ForeignKeyConstraint\s*\(\s*\[[^\]]*\]\s*,\s*\[\s*['\"]([^'\"]+)['\"]",
                    block,
                ):
                    add_fk(module, table, fkm.group(1))

            for am in re.finditer(r"op\.add_column\s*\(\s*['\"]([^'\"]+)['\"]\s*,\s*sa\.Column\s*\(\s*['\"]([^'\"]+)['\"]", text):
                add_col(module, am.group(1), am.group(2))

            for fm in re.finditer(
                r"op\.create_foreign_key\s*\(\s*['\"][^'\"]*['\"]\s*,\s*['\"]([^'\"]+)['\"]\s*,\s*['\"]([^'\"]+)['\"]",
                text,
            ):
                add_fk(module, fm.group(1), fm.group(2))

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
