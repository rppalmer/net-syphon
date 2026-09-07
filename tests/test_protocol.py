"""Real stdio discovery/calls prove the advertised wire contract, not just Python helpers."""

import asyncio
import json
import ssl
import subprocess
import sys
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import jsonschema
import pytest
from mcp import Client, StdioServerParameters

PROJECT_ROOT = Path(__file__).resolve().parents[1]


@contextmanager
def fake_search_server(tls_context=None):
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            requests.append((self.path, self.rfile.read(int(self.headers["content-length"]))))
            body = json.dumps(
                {
                    "results": [
                        {
                            "title": "Example",
                            "url": "https://example.org/",
                            "content": "A search preview",
                            "engine": "hidden",
                        }
                    ]
                }
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    if tls_context is not None:
        server.socket = tls_context.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        scheme = "https" if tls_context is not None else "http"
        yield f"{scheme}://127.0.0.1:{server.server_port}", requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


@pytest.mark.parametrize("configured", [False, True])
@pytest.mark.asyncio
async def test_real_stdio_discovery_and_schema_validated_calls(tmp_path, configured):
    with fake_search_server() as (endpoint, requests):
        environment = {"HOME": str(tmp_path), "PATH": "", "PYTHONUTF8": "1"}
        if configured:
            environment["NET_SYPHON_SEARXNG_URL"] = endpoint
        parameters = StdioServerParameters(
            command=sys.executable,
            args=["-m", "net_syphon"],
            env=environment,
            cwd=str(PROJECT_ROOT),
        )
        async with Client(parameters) as client:
            listed = await client.list_tools()
            assert [tool.name for tool in listed.tools] == [
                "net_syphon_search_web",
                "net_syphon_get_pages",
            ]
            for page_tool in listed.tools[1:]:
                assert page_tool.input_schema["additionalProperties"] is False
                response = await client.call_tool(page_tool.name, {})
                jsonschema.Draft202012Validator(page_tool.output_schema).validate(
                    response.structured_content
                )
                assert response.is_error
            tool = listed.tools[0]
            assert tool.input_schema["additionalProperties"] is False
            assert tool.output_schema is not None
            # Rule 1: no advertised tool may name a provider anywhere in its schema.
            advertised = json.dumps([t.model_dump(mode="json") for t in listed.tools]).lower()
            for backend in ("searxng", "tavily", "firecrawl", "playwright"):
                assert backend not in advertised
            for arguments in (
                {"query": "python"},
                {"query": "!!secret"},
                {"query": "python", "max_results": "5"},
                {"query": "python", "provider": "bad"},
            ):
                result = await client.call_tool(tool.name, arguments)
                jsonschema.Draft202012Validator(tool.output_schema).validate(
                    result.structured_content
                )
                assert json.loads(result.content[0].text) == result.structured_content
                expected_error = arguments != {"query": "python"} or not configured
                assert result.is_error == expected_error
            capabilities = client.server_capabilities
            assert capabilities.tools is not None
            assert capabilities.prompts is None and capabilities.resources is None
        assert len(requests) == (1 if configured else 0)
        logs = list((tmp_path / ".net-syphon/logs").glob("*.jsonl"))
        assert logs and all("secret" not in path.read_text() for path in logs)


@pytest.mark.asyncio
async def test_process_emits_only_json_protocol_and_safe_stderr(tmp_path):
    messages = [
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-11-25",
                "capabilities": {},
                "clientInfo": {"name": "test", "version": "1"},
            },
        },
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
    ]
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "net_syphon",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env={"HOME": str(tmp_path), "PATH": ""},
        cwd=PROJECT_ROOT,
    )
    try:
        assert process.stdin is not None and process.stdout is not None
        process.stdin.write((json.dumps(messages[0]) + "\n").encode())
        await process.stdin.drain()
        initialized = json.loads(await asyncio.wait_for(process.stdout.readline(), 5))
        assert initialized["id"] == 1
        process.stdin.write("".join(json.dumps(m) + "\n" for m in messages[1:]).encode())
        await process.stdin.drain()
        listed = json.loads(await asyncio.wait_for(process.stdout.readline(), 5))
        assert listed["id"] == 2
        assert listed["result"]["tools"][0]["name"] == "net_syphon_search_web"
        # EOF is a shutdown request, so wait for outstanding responses before closing stdin.
        process.stdin.close()
        stdout, stderr = await asyncio.wait_for(process.communicate(), 5)
        assert process.returncode == 0
        assert stdout == b"" and stderr == b""
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()


def test_importing_server_has_no_output_or_home_writes(tmp_path):
    process = subprocess.run(
        [sys.executable, "-c", "import net_syphon.server"],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
        env={"HOME": str(tmp_path), "PATH": ""},
        cwd=PROJECT_ROOT,
    )
    assert process.returncode == 0, process.stderr
    assert process.stdout == "" and process.stderr == ""
    assert list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
async def test_request_ignores_environment_proxy_settings(tmp_path, monkeypatch):
    from net_syphon.service import SearchService

    with (
        fake_search_server() as (endpoint, requests),
        fake_search_server() as (proxy, proxy_requests),
    ):
        monkeypatch.setenv("NET_SYPHON_SEARXNG_URL", endpoint)
        for variable in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
            monkeypatch.setenv(variable, proxy)
        monkeypatch.setenv("NO_PROXY", "")
        result = await SearchService(tmp_path / "private").call({"query": "python"})
        assert result.results and len(requests) == 1
        assert not proxy_requests


@pytest.mark.asyncio
async def test_untrusted_tls_certificate_is_rejected_before_sending_query(tmp_path, monkeypatch):
    from net_syphon.service import SearchService

    key, certificate = tmp_path / "test-key.pem", tmp_path / "test-cert.pem"
    generated = subprocess.run(  # noqa: S603 - fixed executable/flags, isolated pytest paths
        [
            "/usr/bin/openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-keyout",
            str(key),
            "-out",
            str(certificate),
            "-days",
            "1",
            "-subj",
            "/CN=localhost",
        ],
        check=False,
        capture_output=True,
        timeout=10,
    )
    assert generated.returncode == 0
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(certificate, key)
    with fake_search_server(context) as (endpoint, requests):
        monkeypatch.setenv("NET_SYPHON_SEARXNG_URL", endpoint)
        # Even an inherited CA override cannot silently trust this certificate.
        monkeypatch.setenv("SSL_CERT_FILE", str(certificate))
        result = await SearchService(tmp_path / "private").call({"query": "python"})
        assert result.code == "upstream_unavailable"
        assert not requests
