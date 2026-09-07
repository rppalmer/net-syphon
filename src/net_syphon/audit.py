"""Private, bounded JSON-lines audit events with no user content."""

import fcntl
import json
import os
import re
import stat
from contextlib import suppress
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

from net_syphon.clock import SYSTEM_CLOCK, Clock

_COMPONENTS = {
    "policy_check": "policy",
    "dns_start": "policy",
    "dns_end": "policy",
    "page_start": "retrieval",
    "page_end": "retrieval",
    "call_start": "search",
    "call_end": "search",
    "validation_rejected": "search",
    "outbound_start": "http",
    "outbound_end": "http",
    "normalized": "search",
    "cancelled": "search",
    "configuration_rejected": "audit",
}
_NUMERIC_FIELDS = {
    "upstream_captcha_count",
    "upstream_rate_limited_count",
    "upstream_access_denied_count",
    "upstream_timeout_count",
    "page_count",
    "page_index",
    "text_characters",
    "query_length",
    "response_bytes",
    "result_count",
    "rejected_count",
    "upstream_failure_count",
    "duration_ms",
    "http_status",
}
_CODES = {
    "policy_blocked",
    "unsupported_content",
    "no_content",
    "engines_unavailable",
    "invalid_input",
    "not_configured",
    "busy",
    "timeout",
    "rate_limited",
    "access_denied",
    "too_large",
    "upstream_unavailable",
    "internal_error",
    "audit_unavailable",
}
_STATUSES = {"success", "error", "cancelled", "partial"}
_LOG_NAME = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}\.jsonl\Z")


class AuditError(Exception):
    """A safe audit failure that carries no underlying exception details."""

    def __init__(self) -> None:
        super().__init__("Audit logging unavailable.")


def _validate_directory(fd: int) -> None:
    info = os.fstat(fd)
    if (
        not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.getuid()
        or stat.S_IMODE(info.st_mode) != 0o700
    ):
        raise AuditError()


def _open_child_directory(name: str, parent_fd: int) -> int:
    with suppress(FileExistsError):
        os.mkdir(name, mode=0o700, dir_fd=parent_fd)
    fd = os.open(
        name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent_fd
    )
    try:
        _validate_directory(fd)
    except BaseException:
        os.close(fd)
        raise
    return fd


def open_private_directory(path: Path) -> int:
    """Create/open one 0700 owned directory beneath its trusted existing parent.

    The caller owns the returned descriptor. Symlinks at the leaf are rejected;
    no existing permissions are repaired and no ancestor directories are created.
    """
    try:
        if path.name in {"", ".", ".."}:
            raise AuditError()
        parent_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            return _open_child_directory(path.name, parent_fd)
        finally:
            os.close(parent_fd)
    except (OSError, ValueError, TypeError):
        raise AuditError() from None


def open_private_file(name: str, directory_fd: int, *, create: bool = False) -> int:
    """Open an owned, single-link 0600 regular file for read/append.

    Names are single path components. Opens never follow symlinks or wait on a
    FIFO. The caller owns the descriptor and must lock it before mutation.
    """
    try:
        if not isinstance(name, str) or not name or name in {".", ".."} or "/" in name:
            raise AuditError()
        flags = os.O_RDWR | os.O_APPEND | os.O_NOFOLLOW | os.O_NONBLOCK
        if create:
            flags |= os.O_CREAT
        fd = os.open(name, flags, 0o600, dir_fd=directory_fd)
        try:
            info = os.fstat(fd)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) != 0o600
                or info.st_nlink != 1
            ):
                raise AuditError()
        except BaseException:
            os.close(fd)
            raise
        return fd
    except (OSError, ValueError, TypeError):
        raise AuditError() from None


def _serialize_event(event: str, call_id: str, fields: dict, now: datetime) -> bytes:
    if event not in _COMPONENTS or not isinstance(call_id, str) or str(UUID(call_id)) != call_id:
        raise AuditError()
    for name, value in fields.items():
        if name in _NUMERIC_FIELDS:
            if value is not None and (type(value) is not int or not 0 <= value <= 2**63 - 1):
                raise AuditError()
        elif name in {"code", "status"}:
            allowed = _CODES if name == "code" else _STATUSES
            if value is not None and (type(value) is not str or value not in allowed):
                raise AuditError()
        elif name == "reason":
            if value is not None and value not in {
                "network",
                "invalid_json",
                "content_type",
                "encoding",
            }:
                raise AuditError()
        elif name == "parent_call_id":
            if not isinstance(value, str) or str(UUID(value)) != value:
                raise AuditError()
        else:
            raise AuditError()
    record = {
        "event": event,
        "event_id": str(uuid4()),
        "call_id": call_id,
        "timestamp": now.isoformat().replace("+00:00", "Z"),
        "component": _COMPONENTS[event],
        **fields,
    }
    payload = (json.dumps(record, ensure_ascii=True, separators=(",", ":")) + "\n").encode("ascii")
    if len(payload) > 2048:
        raise AuditError()
    return payload


def _prune_logs(directory_fd: int, today: date) -> None:
    cutoff = today - timedelta(days=29)
    for name in os.listdir(directory_fd):
        if not _LOG_NAME.fullmatch(name):
            continue
        try:
            expired = date.fromisoformat(name[:-6]) < cutoff
        except ValueError:
            continue
        if not expired:
            continue
        try:
            fd = open_private_file(name, directory_fd)
        except AuditError:
            continue  # Retention never alters unsafe or unrelated files.
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                continue
            opened = os.fstat(fd)
            try:
                current = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            except FileNotFoundError:
                continue
            if (opened.st_dev, opened.st_ino) == (current.st_dev, current.st_ino):
                os.unlink(name, dir_fd=directory_fd)
        finally:
            os.close(fd)


class AuditWriter:
    """Write a durable event before permitting the caller to proceed."""

    def __init__(
        self,
        root: Path,
        *,
        clock: Clock = SYSTEM_CLOCK,
        max_daily_bytes: int = 10 * 1024 * 1024,
    ) -> None:
        self.root = root
        self.clock = clock
        self.max_daily_bytes = max_daily_bytes
        self._poisoned = False
        self._last_pruned: date | None = None

    def emit(self, event: str, call_id: str, **fields: int | str | bool | None) -> None:
        """Append one allowlisted event; failures are safe and permanently poison this writer."""
        if self._poisoned:
            raise AuditError()
        try:
            if type(self.max_daily_bytes) is not int or self.max_daily_bytes <= 0:
                raise AuditError()
            now = self.clock.now()
            if now.utcoffset() is None:
                raise AuditError()
            now = now.astimezone(UTC)
            payload = _serialize_event(event, call_id, fields, now)
            root_fd = open_private_directory(self.root)
            try:
                logs_fd = _open_child_directory("logs", root_fd)
                try:
                    if self._last_pruned != now.date():
                        _prune_logs(logs_fd, now.date())
                        self._last_pruned = now.date()
                    self._append(logs_fd, f"{now.date()}.jsonl", payload)
                finally:
                    os.close(logs_fd)
            finally:
                os.close(root_fd)
        except Exception:
            self._poisoned = True
            raise AuditError() from None

    def _append(self, logs_fd: int, name: str, payload: bytes) -> None:
        fd = open_private_file(name, logs_fd, create=True)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            size = os.fstat(fd).st_size
            if size + len(payload) > self.max_daily_bytes:
                raise AuditError()
            if size and os.pread(fd, 1, size - 1) != b"\n":
                raise AuditError()
            # One append preserves record boundaries; any short write is fatal.
            if os.write(fd, payload) != len(payload):
                raise AuditError()
            os.fsync(fd)
        finally:
            os.close(fd)
