"""Archived original gate assertions, not a supported or automatically collected test."""

import json
from pathlib import Path

import pytest

from net_syphon.security_probe import run_security_probe


@pytest.mark.security_probe
def test_host_browser_security_gate(tmp_path: Path):
    """Catch browser builds that can escape the per-call filesystem or network boundary."""
    report_path = tmp_path / "security-probe-report.json"

    report = run_security_probe(report_path=report_path)

    assert report.passed, json.dumps(report.model_dump(mode="json"), indent=2)
    assert report_path.is_file()
    assert report_path.stat().st_mode & 0o777 == 0o600
    assert report.checks == {
        "browser_javascript_fixture": True,
        "browser_process_cleanup": True,
        "browser_sandbox_flag": True,
        "canary_read_denied": True,
        "canary_unchanged": True,
        "canary_write_denied": True,
        "direct_dns_denied": True,
        "direct_internet_denied": True,
        "direct_lan_denied": True,
        "direct_quic_denied": True,
        "other_loopback_denied": True,
        "proxy_port_allowed": True,
        "temporary_write_allowed": True,
    }
