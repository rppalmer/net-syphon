"""Archived worker for the failed browser probe; not part of the installed server."""

import argparse
import json
import socket
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright


def _tcp_denied(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=1):
            return False
    except OSError:
        return True


def _udp_denied(host: str, port: int) -> bool:
    udp_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    udp_socket.settimeout(0.5)
    try:
        udp_socket.connect((host, port))
        udp_socket.send(b"net-syphon-security-probe")
    except OSError:
        return True
    finally:
        udp_socket.close()
    return False


def _read_denied(path: Path) -> bool:
    try:
        path.read_bytes()
    except OSError:
        return True
    return False


def _write_denied(path: Path) -> bool:
    try:
        with path.open("ab") as canary_file:
            canary_file.write(b"escape")
    except OSError:
        return True
    return False


def run_worker(arguments: argparse.Namespace) -> dict[str, bool]:
    """Exercise filesystem, socket, and browser behavior from inside Seatbelt."""
    print("STAGE worker-started", flush=True)
    temporary_directory = Path(arguments.temporary_directory)
    canary_paths = [Path(path) for path in arguments.canary]

    checks = {
        "canary_read_denied": all(_read_denied(path) for path in canary_paths),
        "canary_write_denied": all(_write_denied(path) for path in canary_paths),
        "direct_dns_denied": _udp_denied("1.1.1.1", 53),
        "direct_internet_denied": _tcp_denied("1.1.1.1", 80),
        "direct_lan_denied": _tcp_denied("192.168.0.1", 80),
        "direct_quic_denied": _udp_denied("1.1.1.1", 443),
        "other_loopback_denied": _tcp_denied("127.0.0.1", arguments.other_port),
        "proxy_port_allowed": not _tcp_denied("127.0.0.1", arguments.proxy_port),
        "temporary_write_allowed": False,
    }
    print("STAGE basic-checks-complete", flush=True)

    allowed_file = temporary_directory / "allowed-write"
    try:
        allowed_file.write_text("allowed", encoding="utf-8")
        checks["temporary_write_allowed"] = allowed_file.read_text(encoding="utf-8") == "allowed"
    except OSError:
        pass

    checks["browser_javascript_fixture"] = False
    profile_directory = temporary_directory / "browser-profile"
    profile_directory.mkdir()
    try:
        print("STAGE playwright-starting", flush=True)
        with sync_playwright() as playwright:
            print("STAGE playwright-started", flush=True)
            browser = playwright.chromium.launch_persistent_context(
                user_data_dir=profile_directory,
                executable_path=arguments.chromium_executable,
                headless=True,
                chromium_sandbox=True,
                accept_downloads=False,
                service_workers="block",
                proxy={"server": f"http://127.0.0.1:{arguments.proxy_port}"},
                args=[
                    "--disable-background-networking",
                    "--disable-breakpad",
                    "--disable-component-update",
                    "--disable-default-apps",
                    "--disable-features=MediaRouter,OptimizationHints",
                    "--disable-quic",
                    "--disable-sync",
                    "--force-webrtc-ip-handling-policy=disable_non_proxied_udp",
                    "--no-first-run",
                    "--no-service-autorun",
                ],
            )
            print("STAGE chromium-started", flush=True)
            page = browser.pages[0]
            page.goto("http://public.example/security-gate", wait_until="domcontentloaded")
            checks["browser_javascript_fixture"] = (
                page.locator("body").inner_text(timeout=3_000).strip() == "javascript-executed"
            )
            print("READY", flush=True)
            sys.stdin.readline()
            browser.close()
    except Exception as error:
        print(
            json.dumps(
                {
                    "checks": checks,
                    "browser_error_type": type(error).__name__,
                    "browser_error": str(error)[:2_000],
                }
            ),
            flush=True,
        )
        return checks

    print(json.dumps({"checks": checks}), flush=True)
    return checks


def _parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--temporary-directory", required=True)
    parser.add_argument("--proxy-port", required=True, type=int)
    parser.add_argument("--other-port", required=True, type=int)
    parser.add_argument("--chromium-executable", required=True)
    parser.add_argument("--canary", action="append", required=True)
    return parser.parse_args()


if __name__ == "__main__":
    run_worker(_parse_arguments())
