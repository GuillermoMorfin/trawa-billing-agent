"""Command-line entry point: `billing-agent generate | prices | run | app`."""

from __future__ import annotations

import argparse

from billing_agent import __version__
from billing_agent.config import db_path
from billing_agent.db import connect

DEFAULT_MONTH = "2026-09"


def cmd_generate(args: argparse.Namespace) -> int:
    from billing_agent.generate import INJECTED_ISSUES, generate

    with connect(args.db) as con:
        counts = generate(con, args.month)
    print(f"Wrote {counts['customers']} customers and {counts['readings']:,} meter readings")
    print("Planted data problems:")
    for cid, what in INJECTED_ISSUES.items():
        print(f"  {cid}: {what}")
    return 0


def cmd_prices(args: argparse.Namespace) -> int:
    from billing_agent.prices import load_prices

    with connect(args.db) as con:
        n = load_prices(con, args.month, source=args.source)
    print(f"Loaded {n:,} quarter-hour day-ahead prices for {args.month} from {args.source}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="billing-agent", description=__doc__)
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("--db", default=db_path(), help="DuckDB file (default: %(default)s)")
    sub = parser.add_subparsers(dest="command")

    p = sub.add_parser("generate", help="Write demo customers, contracts and meter data.")
    p.add_argument("--month", default=DEFAULT_MONTH, help="Billing month, YYYY-MM.")
    p.set_defaults(func=cmd_generate)

    p = sub.add_parser("prices", help="Load day-ahead prices for the billing month.")
    p.add_argument("--month", default=DEFAULT_MONTH, help="Billing month, YYYY-MM.")
    p.add_argument("--source", choices=["snapshot", "smard"], default="snapshot")
    p.set_defaults(func=cmd_prices)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return 0
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
