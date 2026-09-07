"""Authorize public URLs before sending them to a hosted retriever, never fetch locally."""

import asyncio
import ipaddress
import socket
from urllib.parse import urlsplit, urlunsplit
from uuid import UUID

from net_syphon.audit import AuditWriter
from net_syphon.contracts import ErrorCode, SearchError, valid_web_url


def public_address(value: str) -> bool:
    address = ipaddress.ip_address(value)
    return bool(
        address.is_global
        and not address.is_multicast
        and not address.is_reserved
        and not getattr(address, "ipv4_mapped", None)
    )


async def authorize_url(url: str, audit: AuditWriter, call_id: UUID) -> str:
    """Reject ambiguous syntax/private addresses; DNS preflight is not hosted DNS pinning."""
    audit.emit("policy_check", str(call_id))
    if not valid_web_url(url):
        raise SearchError(ErrorCode.POLICY_BLOCKED)
    parsed = urlsplit(url)
    host = parsed.hostname.encode("idna").decode("ascii").lower()
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    if (
        port != (443 if parsed.scheme == "https" else 80)
        or host.endswith(".")
        or ("." not in host and ":" not in host)
    ):
        raise SearchError(ErrorCode.POLICY_BLOCKED)
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        # Reject legacy numeric spellings such as 0177.0.0.1 and 2130706433.
        try:
            socket.inet_aton(host)
        except OSError:
            pass
        else:
            raise SearchError(ErrorCode.POLICY_BLOCKED) from None
        if host.endswith((".local", ".localhost", ".internal", ".home", ".lan")):
            raise SearchError(ErrorCode.POLICY_BLOCKED) from None
        audit.emit("dns_start", str(call_id))
        try:
            async with asyncio.timeout(5):
                answers = await asyncio.get_running_loop().getaddrinfo(
                    host, port, type=socket.SOCK_STREAM
                )
        except (OSError, TimeoutError):
            audit.emit("dns_end", str(call_id), status="error")
            raise SearchError(ErrorCode.UPSTREAM_UNAVAILABLE) from None
        audit.emit("dns_end", str(call_id), status="success")
        if not answers or any(not public_address(answer[4][0]) for answer in answers):
            raise SearchError(ErrorCode.POLICY_BLOCKED) from None
    else:
        if not public_address(str(address)):
            raise SearchError(ErrorCode.POLICY_BLOCKED)
    hostname = f"[{host}]" if ":" in host else host
    return urlunsplit((parsed.scheme, hostname, parsed.path or "/", parsed.query, ""))
