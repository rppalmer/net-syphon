"""Read-only checks an operator can act on. Never repairs, never prints a credential.

Nothing here creates or modifies a file. A missing log is a healthy state, not a
fault, so doctor reports it rather than fixing it. Connectivity is opt-in and goes
through the same audit boundary as any other request, because rule 2 has no
exception for diagnostics.
"""

import os
import stat
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Literal
from uuid import uuid4

import httpx2 as httpx

from net_syphon.audit import _LOG_NAME, AuditError, AuditWriter
from net_syphon.clock import SYSTEM_CLOCK, Clock
from net_syphon.config import ConfigurationError, load_settings
from net_syphon.contracts import SearchError, SearchRequest

Status = Literal["ok", "warn", "fail", "skipped"]

DAILY_BYTE_CAP = 10 * 1024 * 1024
RETENTION_DAYS = 30


@dataclass(frozen=True)
class Check:
    name: str
    status: Status
    detail: str


def _describe_directory(path: Path) -> tuple[Status, str]:
    try:
        info = os.stat(path, follow_symlinks=False)
    except FileNotFoundError:
        return "warn", f"{path} does not exist yet; it is created on first use."
    except OSError as error:
        return "fail", f"{path} cannot be read ({error.strerror})."
    if not stat.S_ISDIR(info.st_mode):
        return "fail", f"{path} is not a directory."
    if info.st_uid != os.getuid():
        return "fail", f"{path} is owned by another user. Run as its owner or move it."
    mode = stat.S_IMODE(info.st_mode)
    if mode != 0o700:
        return "fail", f"{path} is mode {mode:04o}. Run: chmod 700 {path}"
    return "ok", f"{path} is owned by you and mode 0700."


def _configuration_file(root: Path) -> Check:
    path = root / ".env"
    try:
        info = os.stat(path, follow_symlinks=False)
    except FileNotFoundError:
        return Check(
            "configuration file",
            "ok",
            f"No {path}. Settings come from the environment only, which is supported.",
        )
    except OSError as error:
        return Check("configuration file", "fail", f"{path} cannot be read ({error.strerror}).")
    if not stat.S_ISREG(info.st_mode):
        return Check("configuration file", "fail", f"{path} is not a regular file.")
    if info.st_uid != os.getuid():
        return Check("configuration file", "fail", f"{path} is owned by another user.")
    mode = stat.S_IMODE(info.st_mode)
    if mode != 0o600:
        return Check(
            "configuration file", "fail", f"{path} is mode {mode:04o}. Run: chmod 600 {path}"
        )
    if info.st_nlink != 1:
        return Check("configuration file", "fail", f"{path} has {info.st_nlink} hard links.")
    return Check("configuration file", "ok", f"{path} is owned by you, regular, and mode 0600.")


def _capabilities(root: Path, *, home_exists: bool) -> list[Check]:
    if not home_exists:
        # Reading the dotenv would create the directory that holds it, and a
        # diagnostic that changes state cannot be trusted to report it.
        note = " The home directory does not exist yet, so only the environment was read."
        return [
            Check("ordinary search", "ok", "A search endpoint is set in the environment.")
            if os.environ.get("NET_SYPHON_SEARXNG_URL")
            else Check(
                "ordinary search",
                "fail",
                "Not configured. Set NET_SYPHON_SEARXNG_URL to your instance's base URL, "
                "not its /search endpoint." + note,
            ),
            Check(
                "news, filtered search and retrieval",
                "ok",
                "A retrieval credential is set in the environment. Its value is never shown.",
            )
            if os.environ.get("NET_SYPHON_FIRECRAWL_API_KEY")
            else Check(
                "news, filtered search and retrieval",
                "warn",
                "Not configured. Set NET_SYPHON_FIRECRAWL_API_KEY to enable these. "
                "Ordinary search works without it." + note,
            ),
        ]
    try:
        settings = load_settings(root)
    except ConfigurationError:
        return [
            Check(
                "ordinary search",
                "fail",
                "Configuration could not be read. Check the two checks above.",
            ),
            Check(
                "news, filtered search and retrieval",
                "fail",
                "Configuration could not be read. Check the two checks above.",
            ),
        ]
    search = (
        Check("ordinary search", "ok", "A search endpoint is configured.")
        if settings.searxng_url
        else Check(
            "ordinary search",
            "fail",
            "Not configured. Set NET_SYPHON_SEARXNG_URL to your instance's base URL, "
            "not its /search endpoint.",
        )
    )
    retrieval = (
        Check(
            "news, filtered search and retrieval",
            "ok",
            "A retrieval credential is configured. Its value is never displayed or logged.",
        )
        if settings.firecrawl_api_key
        else Check(
            "news, filtered search and retrieval",
            "warn",
            "Not configured. Set NET_SYPHON_FIRECRAWL_API_KEY to enable these. "
            "Ordinary search works without it.",
        )
    )
    return [search, retrieval]


def _audit_checks(root: Path, today: date) -> list[Check]:
    logs = root / "logs"
    if not logs.exists():
        created = "It is created on the first audited call."
        return [
            Check("audit directory", "ok", f"No {logs} yet. {created}"),
            Check("audit capacity", "ok", f"No daily log yet. {created} The cap is 10 MiB."),
            Check("audit retention", "ok", f"No daily logs yet. {created}"),
        ]
    status, detail = _describe_directory(logs)
    directory = Check("audit directory", status, detail)
    if directory.status == "fail":
        return [
            directory,
            Check("audit capacity", "skipped", "Not checked: the audit directory is unusable."),
            Check("audit retention", "skipped", "Not checked: the audit directory is unusable."),
        ]

    today_file = logs / f"{today}.jsonl"
    try:
        size = os.stat(today_file, follow_symlinks=False).st_size
    except OSError:
        capacity = Check(
            "audit capacity", "ok", f"No log for {today} yet. The daily cap is 10 MiB."
        )
    else:
        remaining = DAILY_BYTE_CAP - size
        if remaining <= 0:
            capacity = Check(
                "audit capacity",
                "fail",
                f"{today}.jsonl has reached the 10 MiB daily cap. No further audited "
                "requests will run today.",
            )
        elif remaining < DAILY_BYTE_CAP // 10:
            capacity = Check(
                "audit capacity",
                "warn",
                f"{today}.jsonl is within {remaining // 1024} KiB of the 10 MiB daily cap.",
            )
        else:
            capacity = Check(
                "audit capacity", "ok", f"{today}.jsonl uses {size // 1024} KiB of the 10 MiB cap."
            )

    try:
        names = sorted(name for name in os.listdir(logs) if _LOG_NAME.fullmatch(name))
    except OSError as error:
        return [
            directory,
            capacity,
            Check("audit retention", "fail", f"{logs} cannot be listed ({error.strerror})."),
        ]
    if not names:
        retention = Check("audit retention", "ok", "No daily logs yet.")
    else:
        expiring = sum(
            1 for name in names if (today - date.fromisoformat(name[:-6])).days >= RETENTION_DAYS
        )
        retention = Check(
            "audit retention",
            "ok",
            f"{len(names)} daily logs, oldest {names[0][:-6]}. "
            + (
                f"{expiring} will be deleted on the next audited call."
                if expiring
                else "None are old enough to be pruned."
            ),
        )
    return [directory, capacity, retention]


async def _connectivity(
    root: Path, clock: Clock, transport: httpx.AsyncBaseTransport | None
) -> list[Check]:
    from net_syphon import firecrawl, searxng

    try:
        settings = load_settings(root)
    except ConfigurationError:
        return [
            Check("search service", "skipped", "Not attempted: configuration is unreadable."),
            Check("retrieval service", "skipped", "Not attempted: configuration is unreadable."),
        ]

    audit = AuditWriter(root, clock=clock)
    try:
        audit.emit("call_start", str(uuid4()))
    except AuditError:
        message = "Not attempted: the audit is unwritable, so egress stays disabled."
        return [
            Check("search service", "skipped", message),
            Check("retrieval service", "skipped", message),
        ]

    checks = []
    request = SearchRequest.model_validate({"query": "net syphon connectivity check"})
    if settings.search_endpoint is None:
        checks.append(Check("search service", "skipped", "Not attempted: not configured."))
    else:
        try:
            batch = await searxng.search(
                settings.search_endpoint, request, audit, uuid4(), transport=transport
            )
        except SearchError as error:
            checks.append(Check("search service", "fail", f"{error.code.value}: {error}"))
        else:
            checks.append(
                Check(
                    "search service",
                    "ok",
                    f"Reachable, returned JSON, {len(batch.results)} usable results.",
                )
            )

    if settings.firecrawl_api_key is None:
        checks.append(Check("retrieval service", "skipped", "Not attempted: not configured."))
    else:
        try:
            await firecrawl.search(
                settings.firecrawl_api_key.get_secret_value(),
                request,
                audit,
                uuid4(),
                clock,
                transport=transport,
            )
        except SearchError as error:
            checks.append(Check("retrieval service", "fail", f"{error.code.value}: {error}"))
        else:
            checks.append(
                Check("retrieval service", "ok", "Reachable and the credential was accepted.")
            )
    return checks


def run_checks(root: Path, *, clock: Clock = SYSTEM_CLOCK) -> list[Check]:
    """Every check that needs no network. Creates nothing and reads no event contents."""
    home_exists = root.exists()
    status, detail = _describe_directory(root)
    if not home_exists:
        detail = (
            f"{root} does not exist yet. The server creates it on first run, "
            f"or create it now: mkdir -m 700 {root}"
        )
    checks = [Check("configuration directory", status, detail)]
    if status == "fail":
        checks.extend(
            Check(name, "skipped", "Not checked: the home directory is unusable.")
            for name in (
                "configuration file",
                "ordinary search",
                "news, filtered search and retrieval",
                "audit directory",
                "audit capacity",
                "audit retention",
            )
        )
        return checks
    checks.append(_configuration_file(root))
    checks.extend(_capabilities(root, home_exists=home_exists))
    checks.extend(_audit_checks(root, clock.now().date()))
    return checks


async def run_connectivity_checks(
    root: Path,
    *,
    clock: Clock = SYSTEM_CLOCK,
    transport: httpx.AsyncBaseTransport | None = None,
) -> list[Check]:
    """One real request per configured capability. Spends one hosted request."""
    return await _connectivity(root, clock, transport)
