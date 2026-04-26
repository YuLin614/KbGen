from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from kbgen.benchmark import benchmark, format_markdown_report
from kbgen.claude_wrapper import run_claude_with_proxy
from kbgen.core import full_scan, incremental_update, init_artifacts


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="kbgen", description="Semantic snapshot generator for cold-start codebase exploration")
    parser.add_argument("--root", default=".", help="Repository root path (default: current directory)")

    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("init", help="Initialize .ai artifacts")
    scan_parser = sub.add_parser("scan", help="Run full cold-start scan")
    scan_parser.add_argument(
        "--path-limit",
        type=int,
        default=0,
        help="Max key file paths per module in snapshot (default: 0, unlimited)",
    )
    update_parser = sub.add_parser("update", help="Run incremental update from git diff")
    update_parser.add_argument(
        "--path-limit",
        type=int,
        default=0,
        help="Max key file paths per module in snapshot (default: 0, unlimited)",
    )

    benchmark_parser = sub.add_parser("benchmark", help="Evaluate A/B run logs for token savings")
    benchmark_parser.add_argument("--baseline", required=True, help="Path to baseline run records (JSON array or JSONL)")
    benchmark_parser.add_argument("--snapshot", required=True, help="Path to with-snapshot run records (JSON array or JSONL)")
    benchmark_parser.add_argument(
        "--output",
        default=".ai/benchmark-report.json",
        help="Output report path (default: .ai/benchmark-report.json)",
    )
    benchmark_parser.add_argument(
        "--markdown",
        action="store_true",
        help="Also generate markdown report",
    )
    benchmark_parser.add_argument(
        "--markdown-output",
        default=".ai/benchmark-report.md",
        help="Markdown report path (default: .ai/benchmark-report.md)",
    )
    benchmark_parser.add_argument(
        "--min-savings",
        type=float,
        default=0.20,
        help="Minimum required median token savings rate (default: 0.20)",
    )
    benchmark_parser.add_argument(
        "--max-success-drop",
        type=float,
        default=0.05,
        help="Maximum allowed success-rate drop (default: 0.05)",
    )
    claude_parser = sub.add_parser("claude", help="Run claude CLI with token usage tracking")
    claude_parser.add_argument(
        "args",
        nargs=argparse.REMAINDER,
        help="Arguments forwarded verbatim to the claude CLI",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    root = Path(args.root).resolve()

    if args.command == "init":
        init_artifacts(root)
        print("initialized .ai artifacts")
        return 0

    if args.command == "scan":
        result = full_scan(
            root,
            key_path_limit=args.path_limit,
        )
        print(json.dumps({"status": "ok", **result}, indent=2))
        return 0

    if args.command == "update":
        result = incremental_update(
            root,
            key_path_limit=args.path_limit,
        )
        print(json.dumps({"status": "ok", **result}, indent=2))
        return 0

    if args.command == "benchmark":
        output_path = Path(args.output).resolve() if args.output else None
        result = benchmark(
            baseline_file=Path(args.baseline).resolve(),
            snapshot_file=Path(args.snapshot).resolve(),
            output_file=output_path,
            min_savings=args.min_savings,
            max_success_drop=args.max_success_drop,
        )
        if args.markdown:
            markdown_path = Path(args.markdown_output).resolve()
            markdown_path.parent.mkdir(parents=True, exist_ok=True)
            markdown_path.write_text(format_markdown_report(result), encoding="utf-8")
            result["markdown_report"] = str(markdown_path)
        if output_path is not None:
            result["json_report"] = str(output_path)
        print(json.dumps({"status": "ok", **result}, indent=2))
        return 0

    if args.command == "claude":
        claude_args = list(args.args)
        if claude_args and claude_args[0] == "--":
            claude_args = claude_args[1:]
        return run_claude_with_proxy(claude_args)

    print(f"unknown command: {args.command}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
