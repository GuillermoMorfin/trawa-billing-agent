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


def cmd_run(args: argparse.Namespace) -> int:
    from billing_agent.prices import missing_price_intervals
    from billing_agent.workflow import run_billing

    with connect(args.db) as con:
        if missing_price_intervals(con, args.month):
            print(f"Prices for {args.month} are incomplete; run `billing-agent prices` first.")
            return 1
        counts = run_billing(con, args.month)
        rows = con.execute(
            """SELECT i.customer_id, c.name, i.status, i.gross_eur,
                      string_agg(s.rule, ', ' ORDER BY s.rule) AS rules
               FROM invoices i JOIN customers c USING (customer_id)
               LEFT JOIN issues s ON s.month = i.month AND s.customer_id = i.customer_id
               WHERE i.month = ?
               GROUP BY ALL ORDER BY i.customer_id""",
            [args.month],
        ).fetchall()
    for cid, name, status, gross, rules in rows:
        amount = f"{gross:>12,.2f} EUR" if gross is not None else " " * 16
        print(f"{cid}  {name[:30]:30}  {status:12}  {amount}  {rules or ''}")
    print(", ".join(f"{n} {s}" for s, n in sorted(counts.items())))
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

    p = sub.add_parser("run", help="Check data and calculate this month's invoices.")
    p.add_argument("--month", default=DEFAULT_MONTH, help="Billing month, YYYY-MM.")
    p.set_defaults(func=cmd_run)
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
