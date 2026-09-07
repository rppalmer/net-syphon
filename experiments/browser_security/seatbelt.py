"""Archived experiment: NOT a supported or verified security boundary."""

from pathlib import Path


def _literal(value: str) -> str:
    if any(character in value for character in ('"', "\n", "\r", "\x00")):
        raise ValueError("Seatbelt profile values may not contain quotes or control characters")
    return f'"{value}"'


def build_browser_profile(
    *, temporary_directory: Path, proxy_port: int, readable_paths: tuple[Path, ...]
) -> str:
    """Return a profile that limits writes to one directory and network to one proxy port."""
    if not 1 <= proxy_port <= 65_535:
        raise ValueError("proxy_port must be between 1 and 65535")

    temporary_directory = temporary_directory.resolve(strict=True)
    resolved_readable_paths = tuple(
        dict.fromkeys(
            (temporary_directory, *(path.resolve(strict=True) for path in readable_paths))
        )
    )
    read_rules = "\n".join(
        f"    (subpath {_literal(str(path))})" for path in resolved_readable_paths
    )

    return f"""(version 1)
(deny default)

; Chromium and the Playwright driver need process and macOS runtime facilities. Filesystem and
; network access remain separately constrained below.
(allow process*)
(allow signal (target self))
(allow sysctl-read)
(allow mach-lookup)
(allow ipc-posix-shm*)
(allow ipc-posix-sem*)
(allow iokit-open)

; Permit path traversal and stat calls, but not file contents outside the explicit subpaths.
(allow file-read-metadata file-test-existence)
(allow file-read*
    (literal "/")
    (literal "/dev/null")
    (subpath "/dev/fd")
{read_rules}
)
(allow file-write* (subpath {_literal(str(temporary_directory))}))
(allow file-write* (literal "/dev/null"))

; The worker and every descendant may talk only to its assigned loopback proxy.
(allow network-outbound (remote ip {_literal(f"localhost:{proxy_port}")}))
"""
