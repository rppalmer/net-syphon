"""The operator-facing adapter. Parses arguments and formats output; no logic lives here.

Running with no arguments starts stdio transport, which is what an MCP host does.
``doctor`` is for the case a host cannot help with: a server that will not start.
"""

import argparse
import asyncio
import logging
import os
import sys
from pathlib import Path

from net_syphon import diagnostics
from net_syphon.server import main as serve

DEFAULT_ROOT = Path.home() / ".net-syphon"
_LABELS = {"ok": "ok  ", "warn": "warn", "fail": "FAIL", "skipped": "--  "}


def _report(checks: list[diagnostics.Check]) -> int:
    width = max(len(check.name) for check in checks)
    for check in checks:
        print(f"  {_LABELS[check.status]}  {check.name.ljust(width)}  {check.detail}")
    failed = sum(1 for check in checks if check.status == "fail")
    warned = sum(1 for check in checks if check.status == "warn")
    print()
    if failed:
        print(f"{failed} problem(s) to fix. Net-Syphon will not work correctly until they are.")
    elif warned:
        print(f"Usable, with {warned} capability or capacity warning(s) above.")
    else:
        print("Everything checked is healthy.")
    return 1 if failed else 0


def _doctor(root: Path, *, connect: bool) -> int:
    print(f"Net-Syphon doctor — {root}\n")
    checks = diagnostics.run_checks(root)
    if connect:
        checks += asyncio.run(diagnostics.run_connectivity_checks(root))
    return _report(checks)


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="net-syphon",
        description="Run the MCP server over stdio, or diagnose this installation.",
    )
    commands = parser.add_subparsers(dest="command")
    doctor = commands.add_parser(
        "doctor", help="Check configuration, permissions and audit health."
    )
    doctor.add_argument(
        "--connect",
        action="store_true",
        help=(
            "Also make one real request per configured capability. "
            "This spends one hosted retrieval request."
        ),
    )
    arguments = parser.parse_args()

    if arguments.command is None:
        serve()
        return

    os.umask(0o077)
    # Doctor's own output is the only thing this process should print.
    logging.disable(logging.CRITICAL)
    try:
        raise SystemExit(_doctor(DEFAULT_ROOT, connect=arguments.connect))
    except KeyboardInterrupt:
        raise SystemExit(1) from None
    except OSError as error:
        print(f"Diagnostics could not complete: {error.strerror}", file=sys.stderr)
        raise SystemExit(1) from None
