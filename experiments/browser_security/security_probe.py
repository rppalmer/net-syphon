"""Archived, failed browser probe. Imports/paths retain the original experimental layout."""

import importlib.metadata
import json
import os
import platform
import socketserver
import subprocess
import sys
import tempfile
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

from playwright.sync_api import sync_playwright
from pydantic import BaseModel, Field

from net_syphon.seatbelt import build_browser_profile

EXPECTED_CHECKS = {
    "browser_javascript_fixture",
    "browser_process_cleanup",
    "browser_sandbox_flag",
    "canary_read_denied",
    "canary_unchanged",
    "canary_write_denied",
    "direct_dns_denied",
    "direct_internet_denied",
    "direct_lan_denied",
    "direct_quic_denied",
    "other_loopback_denied",
    "proxy_port_allowed",
    "temporary_write_allowed",
}


class HostFingerprint(BaseModel):
    """Versions that invalidate a previously passing browser gate when changed."""

    macos_version: str
    macos_build: str
    python_version: str
    playwright_version: str
    chromium_executable: str
    chromium_revision: str


class SecurityProbeReport(BaseModel):
    """Machine-readable result from the browser isolation security gate."""

    passed: bool
    checks: dict[str, bool]
    host: HostFingerprint
    created_at: datetime
    notes: list[str] = Field(default_factory=list)


class _FixtureProxyHandler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        self.connection.settimeout(2)
        request_line = self.rfile.readline(8_193).decode("ascii", errors="replace").strip()
        if not request_line:
            return
        while True:
            header_line = self.rfile.readline(8_193)
            if header_line in {b"\r\n", b"\n", b""}:
                break

        parts = request_line.split(" ")
        if len(parts) != 3 or parts[0] not in {"GET", "HEAD"}:
            self.wfile.write(b"HTTP/1.1 405 Method Not Allowed\r\nContent-Length: 0\r\n\r\n")
            return
        parsed_url = urlsplit(parts[1])
        if parsed_url.hostname != "public.example":
            self.wfile.write(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n\r\n")
            return

        body = (
            b"<!doctype html><title>gate</title><body>not-run</body>"
            b"<script>document.body.textContent='javascript-executed'</script>"
        )
        response_body = b"" if parts[0] == "HEAD" else body
        headers = (
            b"HTTP/1.1 200 OK\r\nContent-Type: text/html; charset=utf-8\r\n"
            + f"Content-Length: {len(response_body)}\r\nConnection: close\r\n\r\n".encode()
        )
        self.wfile.write(headers + response_body)


class _ThreadingServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = False
    daemon_threads = True


def _start_server(handler: type[socketserver.BaseRequestHandler]) -> _ThreadingServer:
    server = _ThreadingServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def _host_fingerprint() -> HostFingerprint:
    with sync_playwright() as playwright:
        executable = Path(playwright.chromium.executable_path).resolve(strict=True)
    revision = next(
        (
            part
            for part in executable.parts
            if part.startswith("chromium-") or "headless_shell-" in part
        ),
        executable.parent.name,
    )
    macos_build = subprocess.run(
        ["/usr/bin/sw_vers", "-buildVersion"],
        check=True,
        capture_output=True,
        text=True,
        timeout=5,
    ).stdout.strip()
    return HostFingerprint(
        macos_version=platform.mac_ver()[0],
        macos_build=macos_build,
        python_version=platform.python_version(),
        playwright_version=importlib.metadata.version("playwright"),
        chromium_executable=str(executable),
        chromium_revision=revision,
    )


def _profile_read_paths(fingerprint: HostFingerprint) -> tuple[Path, ...]:
    project_root = Path(__file__).resolve().parents[2]
    candidates = (
        Path("/System"),
        Path("/usr"),
        Path("/bin"),
        Path("/sbin"),
        Path("/Library/Frameworks/Python.framework"),
        Path(sys.prefix),
        project_root / "src" / "net_syphon",
        Path(fingerprint.chromium_executable).parents[2],
    )
    return tuple(dict.fromkeys(path.resolve(strict=True) for path in candidates))


def _process_commands() -> list[tuple[int, int, str]]:
    output = subprocess.run(
        ["/bin/ps", "-axo", "pid=,ppid=,command="],
        check=True,
        capture_output=True,
        text=True,
        timeout=5,
    ).stdout
    processes: list[tuple[int, int, str]] = []
    for line in output.splitlines():
        parts = line.strip().split(maxsplit=2)
        if len(parts) == 3:
            processes.append((int(parts[0]), int(parts[1]), parts[2]))
    return processes


def _descendant_commands(parent_pid: int) -> list[str]:
    processes = _process_commands()
    descendants = {parent_pid}
    changed = True
    while changed:
        changed = False
        for process_id, parent_id, _command in processes:
            if parent_id in descendants and process_id not in descendants:
                descendants.add(process_id)
                changed = True
    return [command for process_id, _parent_id, command in processes if process_id in descendants]


def _write_report(path: Path, report: SecurityProbeReport) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags, 0o600)
    try:
        os.fchmod(descriptor, 0o600)
        payload = report.model_dump_json(indent=2).encode("utf-8") + b"\n"
        os.write(descriptor, payload)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _prepare_canaries(project_root: Path) -> list[tuple[Path, bytes]]:
    locations = [
        project_root / ".security-probe-canary",
        project_root.parent / "ORIS" / ".security-probe-canary",
        Path.home() / ".net-syphon" / ".security-probe-canary",
    ]
    canaries: list[tuple[Path, bytes]] = []
    for index, path in enumerate(locations):
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if path.exists():
            raise RuntimeError(f"Refusing to replace existing security canary: {path}")
        content = f"net-syphon-canary-{index}".encode()
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            os.write(descriptor, content)
        finally:
            os.close(descriptor)
        canaries.append((path, content))
    return canaries


def _parse_worker_result(output_lines: list[str]) -> tuple[dict[str, bool], list[str]]:
    notes: list[str] = []
    for line in reversed(output_lines):
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if error_type := payload.get("browser_error_type"):
            notes.append(f"Browser worker failed with {error_type}")
        if browser_error := payload.get("browser_error"):
            notes.append(f"Browser error: {browser_error}")
        checks = payload.get("checks")
        if isinstance(checks, dict):
            return {str(key): bool(value) for key, value in checks.items()}, notes
    notes.append("Browser worker did not return a JSON result")
    return {}, notes


def run_security_probe(*, report_path: Path) -> SecurityProbeReport:
    """Run the security gate and write its result for runtime attestation."""
    if platform.system() != "Darwin" or not Path("/usr/bin/sandbox-exec").is_file():
        raise RuntimeError("The v1 browser security gate requires macOS sandbox-exec")

    fingerprint = _host_fingerprint()
    project_root = Path(__file__).resolve().parents[2]
    canaries = _prepare_canaries(project_root)
    proxy_server = _start_server(_FixtureProxyHandler)
    other_server = _start_server(socketserver.BaseRequestHandler)
    checks: dict[str, bool] = {}
    notes: list[str] = []

    try:
        with tempfile.TemporaryDirectory(prefix="net-syphon-security-") as temporary_name:
            temporary_directory = Path(temporary_name).resolve()
            profile_path = temporary_directory / "browser.sb"
            profile_path.write_text(
                build_browser_profile(
                    temporary_directory=temporary_directory,
                    proxy_port=proxy_server.server_address[1],
                    readable_paths=_profile_read_paths(fingerprint),
                ),
                encoding="utf-8",
            )
            worker_command = [
                "/usr/bin/sandbox-exec",
                "-f",
                str(profile_path),
                sys.executable,
                str(project_root / "src" / "net_syphon" / "probe_worker.py"),
                "--temporary-directory",
                str(temporary_directory),
                "--proxy-port",
                str(proxy_server.server_address[1]),
                "--other-port",
                str(other_server.server_address[1]),
                "--chromium-executable",
                fingerprint.chromium_executable,
            ]
            for canary_path, _content in canaries:
                worker_command.extend(["--canary", str(canary_path)])

            environment = {
                "HOME": str(temporary_directory),
                "PATH": "/usr/bin:/bin",
                "PYTHONNOUSERSITE": "1",
                "TMPDIR": str(temporary_directory),
            }
            worker = subprocess.Popen(  # noqa: S603 - fixed executable and arguments
                worker_command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=environment,
                cwd=temporary_directory,
                start_new_session=True,
            )
            output_lines: list[str] = []
            assert worker.stdout is not None
            deadline = time.monotonic() + 30
            ready = False
            while time.monotonic() < deadline:
                line = worker.stdout.readline()
                if not line:
                    break
                output_lines.append(line.strip())
                if line.strip() == "READY":
                    ready = True
                    break

            commands = _descendant_commands(worker.pid) if ready else []
            chromium_commands = [command for command in commands if "Chrome" in command]
            checks["browser_sandbox_flag"] = bool(chromium_commands) and not any(
                "--no-sandbox" in command for command in chromium_commands
            )

            if worker.stdin is not None and worker.poll() is None:
                worker.stdin.write("continue\n")
                worker.stdin.flush()
            try:
                remaining_stdout, stderr = worker.communicate(timeout=10)
            except subprocess.TimeoutExpired:
                os.killpg(worker.pid, 9)
                remaining_stdout, stderr = worker.communicate(timeout=5)
                notes.append("Worker exceeded cleanup deadline and was killed")
            output_lines.extend(line.strip() for line in remaining_stdout.splitlines())
            worker_checks, worker_notes = _parse_worker_result(output_lines)
            checks.update(worker_checks)
            notes.extend(worker_notes)
            stages = [line for line in output_lines if line.startswith("STAGE ")]
            if stages:
                notes.append(f"Worker stages: {', '.join(stages)}")
            if worker.returncode:
                notes.append(f"Worker exited with status {worker.returncode}")
            if stderr.strip():
                notes.append(f"Worker stderr: {stderr.strip()[:2_000]}")

            profile_marker = str(temporary_directory / "browser-profile")
            checks["browser_process_cleanup"] = not any(
                profile_marker in command for _pid, _ppid, command in _process_commands()
            )
            checks["canary_unchanged"] = all(
                path.read_bytes() == content for path, content in canaries
            )
    finally:
        proxy_server.shutdown()
        proxy_server.server_close()
        other_server.shutdown()
        other_server.server_close()
        for path, _content in canaries:
            path.unlink(missing_ok=True)

    normalized_checks = {name: checks.get(name, False) for name in sorted(EXPECTED_CHECKS)}
    report = SecurityProbeReport(
        passed=all(normalized_checks.values()),
        checks=normalized_checks,
        host=fingerprint,
        created_at=datetime.now(UTC),
        notes=notes,
    )
    _write_report(report_path, report)
    return report


def main() -> None:
    """Run the browser security gate from the command line."""
    report_path = Path.home() / ".net-syphon" / "security-probe.json"
    report = run_security_probe(report_path=report_path)
    print(report.model_dump_json(indent=2))
    raise SystemExit(0 if report.passed else 1)
