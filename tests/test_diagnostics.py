"""Doctor reports what an operator can act on, and never prints a credential."""

import pytest

from net_syphon.diagnostics import run_checks


def healthy_root(tmp_path):
    root = tmp_path / "runtime"
    root.mkdir(mode=0o700)
    (root / "logs").mkdir(mode=0o700)
    return root


def by_name(checks):
    return {check.name: check for check in checks}


def test_fully_configured_installation_reports_no_failures(tmp_path, monkeypatch):
    monkeypatch.setenv("NET_SYPHON_SEARXNG_URL", "http://192.0.2.10:8080")
    monkeypatch.setenv("NET_SYPHON_FIRECRAWL_API_KEY", "a-test-key")

    checks = run_checks(healthy_root(tmp_path))

    assert [check.name for check in checks if check.status == "fail"] == []
    assert by_name(checks)["ordinary search"].status == "ok"
    assert by_name(checks)["news, filtered search and retrieval"].status == "ok"


def test_wrong_home_permissions_fail_with_an_actionable_command(tmp_path):
    root = tmp_path / "runtime"
    root.mkdir(mode=0o755)

    checks = run_checks(root)

    directory = by_name(checks)["configuration directory"]
    assert directory.status == "fail"
    assert "chmod 700" in directory.detail
    # Nothing downstream is guessed at once the home directory is unusable.
    assert {c.status for c in checks[1:]} == {"skipped"}


def test_missing_search_endpoint_fails_but_missing_key_only_warns(tmp_path, monkeypatch):
    monkeypatch.delenv("NET_SYPHON_SEARXNG_URL", raising=False)
    monkeypatch.delenv("NET_SYPHON_FIRECRAWL_API_KEY", raising=False)

    checks = by_name(run_checks(healthy_root(tmp_path)))

    # Ordinary search is the baseline capability; retrieval is optional by design.
    assert checks["ordinary search"].status == "fail"
    assert "NET_SYPHON_SEARXNG_URL" in checks["ordinary search"].detail
    assert checks["news, filtered search and retrieval"].status == "warn"


def test_no_check_ever_reveals_the_retrieval_credential(tmp_path, monkeypatch):
    monkeypatch.setenv("NET_SYPHON_SEARXNG_URL", "http://192.0.2.10:8080")
    monkeypatch.setenv("NET_SYPHON_FIRECRAWL_API_KEY", "super-secret-value")

    checks = run_checks(healthy_root(tmp_path))
    rendered = " ".join(f"{c.name} {c.status} {c.detail}" for c in checks)

    assert "super-secret-value" not in rendered
    assert "secret" not in rendered


def test_a_full_daily_log_is_reported_as_a_failure(tmp_path, monkeypatch):
    from datetime import UTC, datetime

    from net_syphon.clock import FixedClock

    monkeypatch.setenv("NET_SYPHON_SEARXNG_URL", "http://192.0.2.10:8080")
    root = healthy_root(tmp_path)
    moment = datetime(2026, 9, 7, 12, tzinfo=UTC)
    full = root / "logs" / "2026-09-07.jsonl"
    full.write_bytes(b"x" * (10 * 1024 * 1024))
    full.chmod(0o600)

    checks = by_name(run_checks(root, clock=FixedClock(moment)))

    assert checks["audit capacity"].status == "fail"
    assert "daily cap" in checks["audit capacity"].detail


def test_retention_names_the_logs_the_next_call_will_delete(tmp_path):
    from datetime import UTC, datetime

    from net_syphon.clock import FixedClock

    root = healthy_root(tmp_path)
    for name in ("2026-07-01.jsonl", "2026-09-06.jsonl"):
        path = root / "logs" / name
        path.write_text("")
        path.chmod(0o600)

    checks = by_name(run_checks(root, clock=FixedClock(datetime(2026, 9, 7, tzinfo=UTC))))

    assert checks["audit retention"].status == "ok"
    assert "2026-07-01" in checks["audit retention"].detail
    assert "1 will be deleted" in checks["audit retention"].detail


@pytest.mark.asyncio
async def test_connectivity_reports_reachable_services(tmp_path, monkeypatch):
    import httpx2 as httpx

    from net_syphon.diagnostics import run_connectivity_checks

    monkeypatch.setenv("NET_SYPHON_SEARXNG_URL", "http://192.0.2.10:8080")
    monkeypatch.setenv("NET_SYPHON_FIRECRAWL_API_KEY", "a-test-key")

    def handler(request):
        if "firecrawl" in str(request.url):
            return httpx.Response(
                200,
                json={"success": True, "data": {"web": [{"title": "T", "url": "https://a.test/"}]}},
            )
        return httpx.Response(200, json={"results": [{"title": "T", "url": "https://a.test/"}]})

    checks = by_name(
        await run_connectivity_checks(
            healthy_root(tmp_path), transport=httpx.MockTransport(handler)
        )
    )

    assert checks["search service"].status == "ok"
    assert "1 usable results" in checks["search service"].detail
    assert checks["retrieval service"].status == "ok"


@pytest.mark.asyncio
async def test_connectivity_classifies_a_rejected_credential(tmp_path, monkeypatch):
    import httpx2 as httpx

    from net_syphon.diagnostics import run_connectivity_checks

    monkeypatch.delenv("NET_SYPHON_SEARXNG_URL", raising=False)
    monkeypatch.setenv("NET_SYPHON_FIRECRAWL_API_KEY", "a-test-key")
    transport = httpx.MockTransport(lambda request: httpx.Response(401, json={}))

    checks = by_name(await run_connectivity_checks(healthy_root(tmp_path), transport=transport))

    assert checks["search service"].status == "skipped"
    assert checks["retrieval service"].status == "fail"
    assert "access_denied" in checks["retrieval service"].detail


@pytest.mark.asyncio
async def test_connectivity_refuses_to_run_when_the_audit_is_unwritable(tmp_path, monkeypatch):
    """Doctor obeys the server's own rule: no audit, no egress."""
    import httpx2 as httpx

    from net_syphon.diagnostics import run_connectivity_checks

    monkeypatch.setenv("NET_SYPHON_SEARXNG_URL", "http://192.0.2.10:8080")
    monkeypatch.setenv("NET_SYPHON_FIRECRAWL_API_KEY", "a-test-key")
    root = tmp_path / "runtime"
    root.mkdir(mode=0o700)
    # A regular file where the log directory belongs makes the audit unusable.
    (root / "logs").write_text("")

    def unexpected(request):
        pytest.fail("Doctor made a request with the audit unavailable")

    checks = by_name(await run_connectivity_checks(root, transport=httpx.MockTransport(unexpected)))

    assert checks["search service"].status == "skipped"
    assert checks["retrieval service"].status == "skipped"
    assert "egress stays disabled" in checks["search service"].detail


def test_doctor_creates_nothing(tmp_path, monkeypatch):
    """A diagnostic that repairs state cannot be trusted to report it."""
    monkeypatch.delenv("NET_SYPHON_SEARXNG_URL", raising=False)
    monkeypatch.delenv("NET_SYPHON_FIRECRAWL_API_KEY", raising=False)
    root = tmp_path / "never-created"

    run_checks(root)

    assert not root.exists()


def run_cli(tmp_path, *arguments, **environment):
    import subprocess
    import sys
    from pathlib import Path

    return subprocess.run(  # noqa: S603 - fixed interpreter, literal arguments
        [sys.executable, "-m", "net_syphon", *arguments],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
        env={"HOME": str(tmp_path), "PATH": "", "PYTHONUTF8": "1", **environment},
        cwd=str(Path(__file__).resolve().parents[1]),
    )


def test_doctor_command_reports_every_check_and_fails_when_unconfigured(tmp_path):
    result = run_cli(tmp_path, "doctor")
    assert result.returncode == 1, result.stderr
    for name in ("configuration directory", "ordinary search", "audit retention"):
        assert name in result.stdout
    # Doctor is a report, not a repair.
    assert not (tmp_path / ".net-syphon").exists()


def test_doctor_command_succeeds_once_search_is_configured(tmp_path):
    (tmp_path / ".net-syphon").mkdir(mode=0o700)
    result = run_cli(tmp_path, "doctor", NET_SYPHON_SEARXNG_URL="http://192.0.2.10:8080")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "FAIL" not in result.stdout
