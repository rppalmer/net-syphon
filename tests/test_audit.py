"""Filesystem and data-minimization contracts for the local audit log."""

import fcntl
import json
import multiprocessing
import os
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest

from net_syphon import audit
from net_syphon.clock import FixedClock

CALL_ID = "c453bc84-c268-4a91-8daf-c7eac6a4b4e9"
NOW = datetime(2026, 9, 5, 12, tzinfo=UTC)


def writer_for(root, **kwargs):
    return audit.AuditWriter(root, clock=FixedClock(NOW), **kwargs)


def private_file(path, contents=b""):
    path.write_bytes(contents)
    path.chmod(0o600)


def test_emit_writes_private_structured_utc_event(tmp_path):
    from net_syphon.audit import AuditWriter

    root = tmp_path / "runtime"
    writer = AuditWriter(root, clock=FixedClock(datetime(2026, 9, 5, 12, tzinfo=UTC)))
    assert not root.exists()
    writer.emit("call_start", "c453bc84-c268-4a91-8daf-c7eac6a4b4e9", query_length=12)

    log = root / "logs" / "2026-09-05.jsonl"
    event = json.loads(log.read_text())
    assert event["event"] == "call_start"
    assert event["component"] == "search"
    assert event["timestamp"] == "2026-09-05T12:00:00Z"
    assert event["query_length"] == 12
    assert event["call_id"] == "c453bc84-c268-4a91-8daf-c7eac6a4b4e9"
    assert UUID(event["event_id"]).version == 4
    assert root.stat().st_mode & 0o7777 == 0o700
    assert log.parent.stat().st_mode & 0o7777 == 0o700
    assert log.stat().st_mode & 0o7777 == 0o600


@pytest.mark.parametrize(
    ("event", "call_id", "fields"),
    [
        ("secret-query", CALL_ID, {}),
        ("call_start", "secret-query", {}),
        ("call_start", CALL_ID, {"query": "secret-query"}),
        ("call_start", CALL_ID, {"status": "secret-query"}),
        ("call_start", CALL_ID, {"code": "secret-query"}),
        ("call_start", CALL_ID, {"query_length": "secret-query"}),
        ("call_start", CALL_ID, {"query_length": True}),
        ("call_start", CALL_ID, {"query_length": -1}),
        ("call_start", CALL_ID, {"query_length": 10**1000}),
        ("call_start", CALL_ID, {"component": "secret-query"}),
    ],
)
def test_rejects_non_allowlisted_data_without_persisting_it(tmp_path, event, call_id, fields):
    root = tmp_path / "runtime"
    with pytest.raises(audit.AuditError) as caught:
        writer_for(root).emit(event, call_id, **fields)
    assert "secret-query" not in str(caught.value)
    assert not root.exists()


@pytest.mark.parametrize(
    ("event", "component"),
    [("outbound_start", "http"), ("configuration_rejected", "audit"), ("cancelled", "search")],
)
def test_component_is_derived_from_event(tmp_path, event, component):
    writer_for(tmp_path / "runtime").emit(event, CALL_ID, status="error", code="timeout")
    record = json.loads((tmp_path / "runtime/logs/2026-09-05.jsonl").read_bytes())
    assert record["component"] == component


@pytest.mark.parametrize("boundary", ["root", "logs", "file"])
def test_refuses_symlink_boundaries(tmp_path, boundary):
    root = tmp_path / "runtime"
    target = tmp_path / "target"
    target.mkdir(mode=0o700)
    if boundary == "root":
        root.symlink_to(target, target_is_directory=True)
    else:
        root.mkdir(mode=0o700)
        if boundary == "logs":
            (root / "logs").symlink_to(target, target_is_directory=True)
        else:
            (root / "logs").mkdir(mode=0o700)
            private_file(target / "secret", b"untouched")
            (root / "logs/2026-09-05.jsonl").symlink_to(target / "secret")
    with pytest.raises(audit.AuditError):
        writer_for(root).emit("call_start", CALL_ID)
    if boundary == "file":
        assert (target / "secret").read_bytes() == b"untouched"
    else:
        assert list(target.iterdir()) == []


@pytest.mark.parametrize("boundary", ["root", "logs", "file"])
def test_refuses_insecure_modes(tmp_path, boundary):
    root = tmp_path / "runtime"
    logs = root / "logs"
    root.mkdir(mode=0o700)
    logs.mkdir(mode=0o700)
    log = logs / "2026-09-05.jsonl"
    private_file(log)
    {"root": root, "logs": logs, "file": log}[boundary].chmod(0o755)
    with pytest.raises(audit.AuditError):
        writer_for(root).emit("call_start", CALL_ID)
    assert log.read_bytes() == b""


def test_refuses_hardlinked_log(tmp_path):
    root = tmp_path / "runtime"
    writer_for(root).emit("call_start", CALL_ID)
    log = root / "logs/2026-09-05.jsonl"
    original = log.read_bytes()
    os.link(log, tmp_path / "second-link")
    with pytest.raises(audit.AuditError):
        writer_for(root).emit("call_end", CALL_ID)
    assert log.read_bytes() == original


def test_refuses_wrong_owner(tmp_path, monkeypatch):
    root = tmp_path / "runtime"
    writer_for(root).emit("call_start", CALL_ID)
    log = root / "logs/2026-09-05.jsonl"
    original = log.read_bytes()
    monkeypatch.setattr(audit.os, "getuid", lambda: 9999999)
    with pytest.raises(audit.AuditError):
        writer_for(root).emit("call_end", CALL_ID)
    assert log.read_bytes() == original


def test_refuses_fifo_without_waiting_for_a_reader(tmp_path):
    root = tmp_path / "runtime"
    root.mkdir(mode=0o700)
    (root / "logs").mkdir(mode=0o700)
    os.mkfifo(root / "logs/2026-09-05.jsonl", 0o600)
    with pytest.raises(audit.AuditError):
        writer_for(root).emit("call_start", CALL_ID)


def test_daily_cap_preserves_existing_bytes(tmp_path):
    root = tmp_path / "runtime"
    writer_for(root).emit("call_start", CALL_ID)
    log = root / "logs/2026-09-05.jsonl"
    original = log.read_bytes()
    with pytest.raises(audit.AuditError):
        writer_for(root, max_daily_bytes=len(original) + 10).emit("call_end", CALL_ID)
    assert log.read_bytes() == original


def test_refuses_to_append_after_a_previous_partial_record(tmp_path):
    root = tmp_path / "runtime"
    root.mkdir(mode=0o700)
    (root / "logs").mkdir(mode=0o700)
    log = root / "logs/2026-09-05.jsonl"
    private_file(log, b'{"event":')
    with pytest.raises(audit.AuditError):
        writer_for(root).emit("call_start", CALL_ID)
    assert log.read_bytes() == b'{"event":'


def test_rejects_naive_clock_before_creating_logs(tmp_path):
    root = tmp_path / "runtime"
    writer = audit.AuditWriter(root, clock=FixedClock(datetime(2026, 9, 5)))
    with pytest.raises(audit.AuditError):
        writer.emit("call_start", CALL_ID)
    assert not root.exists()


@pytest.mark.parametrize("limit", [True, -1, 0, 1024.5])
def test_rejects_invalid_size_limit_before_creating_logs(tmp_path, limit):
    root = tmp_path / "runtime"
    with pytest.raises(audit.AuditError):
        writer_for(root, max_daily_bytes=limit).emit("call_start", CALL_ID)
    assert not root.exists()


def test_private_directory_creates_only_leaf_and_returns_validated_fd(tmp_path):
    root = tmp_path / "runtime"
    fd = audit.open_private_directory(root)
    try:
        assert os.fstat(fd).st_ino == root.stat().st_ino
    finally:
        os.close(fd)
    with pytest.raises(audit.AuditError):
        audit.open_private_directory(tmp_path / "missing/child")
    assert not (tmp_path / "missing").exists()


def test_private_file_cannot_escape_directory_or_create_without_request(tmp_path):
    root = tmp_path / "runtime"
    fd = audit.open_private_directory(root)
    try:
        for name in ["../escape", "/escape", "..", ".", ""]:
            with pytest.raises(audit.AuditError):
                audit.open_private_file(name, fd, create=True)
        with pytest.raises(audit.AuditError):
            audit.open_private_file("missing", fd)
        assert list(root.iterdir()) == []
    finally:
        os.close(fd)


def test_locked_file_fails_without_appending(tmp_path):
    root = tmp_path / "runtime"
    writer_for(root).emit("call_start", CALL_ID)
    log = root / "logs/2026-09-05.jsonl"
    original = log.read_bytes()
    with log.open("rb") as held:
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(audit.AuditError):
            writer_for(root).emit("call_end", CALL_ID)
    assert log.read_bytes() == original


@pytest.mark.parametrize("failure", ["write", "partial", "fsync"])
def test_disk_failures_poison_writer_and_hide_exception(tmp_path, monkeypatch, failure):
    root = tmp_path / "runtime"
    writer = writer_for(root)
    real_write = os.write

    def disk_failure(*args):
        raise OSError("secret-disk-detail")

    def partial_write(fd, payload):
        return real_write(fd, payload[:5])

    with monkeypatch.context() as patch:
        if failure == "partial":
            patch.setattr(audit.os, "write", partial_write)
        else:
            patch.setattr(audit.os, failure, disk_failure)
        with pytest.raises(audit.AuditError) as caught:
            writer.emit("call_start", CALL_ID)
        assert "secret" not in str(caught.value)
    log = root / "logs/2026-09-05.jsonl"
    original = log.read_bytes()
    with pytest.raises(audit.AuditError):
        writer.emit("call_end", CALL_ID)
    assert log.read_bytes() == original


def test_retention_preserves_last_30_days_and_unrelated_or_unsafe_files(tmp_path):
    root = tmp_path / "runtime"
    logs = root / "logs"
    root.mkdir(mode=0o700)
    logs.mkdir(mode=0o700)
    for name in ["2026-08-06.jsonl", "2026-08-07.jsonl", "2026-09-06.jsonl", "notes.txt"]:
        private_file(logs / name, b"keep-or-delete")
    private_file(logs / "2026-08-05.jsonl", b"insecure")
    (logs / "2026-08-05.jsonl").chmod(0o644)
    (logs / "2026-08-04.jsonl").symlink_to(logs / "notes.txt")
    private_file(logs / "2026-08-03.jsonl", b"hardlinked")
    os.link(logs / "2026-08-03.jsonl", logs / "linked.txt")
    private_file(logs / "2026-08-02.jsonl", b"locked")
    with (logs / "2026-08-02.jsonl").open("rb") as held:
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        writer_for(root).emit("call_start", CALL_ID)
    assert not (logs / "2026-08-06.jsonl").exists()
    for name in [
        "2026-08-07.jsonl",
        "2026-09-06.jsonl",
        "notes.txt",
        "2026-08-05.jsonl",
        "2026-08-04.jsonl",
        "2026-08-03.jsonl",
        "2026-08-02.jsonl",
        "linked.txt",
    ]:
        assert (logs / name).exists()


def test_retention_runs_at_most_once_per_utc_day(tmp_path):
    root = tmp_path / "runtime"

    class AdvancingClock:
        moment = NOW

        def now(self):
            return self.moment

    current = AdvancingClock()
    writer = audit.AuditWriter(root, clock=current)
    writer.emit("call_start", CALL_ID)
    old = root / "logs/2026-08-01.jsonl"
    private_file(old)
    writer.emit("call_end", CALL_ID)
    assert old.exists()
    current.moment += timedelta(days=1)
    writer.emit("call_start", CALL_ID)
    assert not old.exists()


def append_from_process(root, count, queue):
    successes = 0
    for _ in range(count):
        try:
            writer_for(root).emit("call_start", CALL_ID)
        except audit.AuditError:
            pass  # Nonblocking contention intentionally fails closed.
        else:
            successes += 1
    queue.put(successes)


def test_concurrent_processes_only_append_complete_events(tmp_path):
    root = tmp_path / "runtime"
    context = multiprocessing.get_context("spawn")
    queue = context.Queue()
    processes = [
        context.Process(target=append_from_process, args=(root, 15, queue)) for _ in range(3)
    ]
    for process in processes:
        process.start()
    for process in processes:
        process.join(timeout=15)
        assert process.exitcode == 0
    successes = sum(queue.get(timeout=2) for _ in processes)
    events = [
        json.loads(line) for line in (root / "logs/2026-09-05.jsonl").read_bytes().splitlines()
    ]
    assert len(events) == successes
    assert len(events) > 0
    assert len({event["event_id"] for event in events}) == len(events)
