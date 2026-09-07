"""Configuration never loads repo dotenv files or insecure operator files."""

import os

import pytest


def test_environment_overrides_operator_file(tmp_path, monkeypatch):
    from net_syphon.config import load_settings

    root = tmp_path / ".net-syphon"
    root.mkdir(mode=0o700)
    dotenv = root / ".env"
    dotenv.write_text("NET_SYPHON_SEARXNG_URL=http://192.168.1.2:8080/\n")
    dotenv.chmod(0o600)
    monkeypatch.setenv("NET_SYPHON_SEARXNG_URL", "https://search.example.org/prefix/")
    assert load_settings(root).search_endpoint == "https://search.example.org/prefix/search"


def test_uses_only_operator_dotenv_and_allows_missing_config(tmp_path, monkeypatch):
    from net_syphon.config import load_settings

    monkeypatch.delenv("NET_SYPHON_SEARXNG_URL", raising=False)
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text("NET_SYPHON_SEARXNG_URL=http://untrusted.invalid\n")
    root = tmp_path / ".net-syphon"
    assert load_settings(root).search_endpoint is None
    dotenv = root / ".env"
    dotenv.write_text("NET_SYPHON_SEARXNG_URL=http://192.168.1.2:8080\n")
    dotenv.chmod(0o600)
    assert load_settings(root).search_endpoint == "http://192.168.1.2:8080/search"


@pytest.mark.parametrize(
    "url",
    [
        "ftp://example.org",
        "http://u:p@example.org",
        "http://host/?q=x",
        "http://host/#part",
        "http://host/#",
        "http://host/?",
        "http://host/\n",
    ],
)
def test_rejects_unsafe_operator_endpoint(tmp_path, monkeypatch, url):
    from net_syphon.config import ConfigurationError, load_settings

    monkeypatch.setenv("NET_SYPHON_SEARXNG_URL", url)
    with pytest.raises(ConfigurationError, match="configuration"):
        load_settings(tmp_path / ".net-syphon")


@pytest.mark.parametrize("kind", ["symlink", "world_readable", "hardlink", "fifo", "large"])
def test_refuses_unsafe_dotenv(tmp_path, monkeypatch, kind):
    from net_syphon.config import ConfigurationError, load_settings

    monkeypatch.delenv("NET_SYPHON_SEARXNG_URL", raising=False)
    root = tmp_path / ".net-syphon"
    root.mkdir(mode=0o700)
    dotenv = root / ".env"
    if kind == "symlink":
        target = tmp_path / "secret"
        target.write_text("private-value")
        dotenv.symlink_to(target)
    elif kind == "fifo":
        os.mkfifo(dotenv, 0o600)
    else:
        dotenv.write_text("x" * (16_385 if kind == "large" else 10))
        dotenv.chmod(0o644 if kind == "world_readable" else 0o600)
        if kind == "hardlink":
            os.link(dotenv, tmp_path / "alias")
    with pytest.raises(ConfigurationError):
        load_settings(root)
