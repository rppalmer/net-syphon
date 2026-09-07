"""Read the operator's protected configuration, without searching the working directory."""

import os
import stat
from io import StringIO
from pathlib import Path

from dotenv import dotenv_values
from pydantic import SecretStr, ValidationError, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from net_syphon.audit import AuditError, open_private_directory
from net_syphon.contracts import valid_web_url


class ConfigurationError(Exception):
    def __init__(self) -> None:
        super().__init__("Invalid or inaccessible operator configuration")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="NET_SYPHON_", env_file=None, extra="forbid", hide_input_in_errors=True
    )
    searxng_url: str | None = None
    firecrawl_api_key: SecretStr | None = None

    @field_validator("firecrawl_api_key")
    @classmethod
    def validate_key(cls, value: SecretStr | None) -> SecretStr | None:
        if value is not None:
            key = value.get_secret_value()
            if not 1 <= len(key) <= 512 or any(ord(char) < 33 or ord(char) > 126 for char in key):
                raise ValueError("Invalid retrieval credential")
        return value

    @field_validator("searxng_url")
    @classmethod
    def validate_endpoint(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if not valid_web_url(value) or "?" in value or "#" in value:
            raise ValueError("Invalid search endpoint")
        return value.rstrip("/")

    @property
    def search_endpoint(self) -> str | None:
        return f"{self.searxng_url}/search" if self.searxng_url else None


def load_settings(root: Path) -> Settings:
    """Validate even an overridden dotenv file; environment wins only after safe reading."""
    try:
        directory = open_private_directory(root)
        try:
            try:
                descriptor = os.open(
                    ".env", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory
                )
            except FileNotFoundError:
                contents = ""
            else:
                try:
                    info = os.fstat(descriptor)
                    if (
                        not stat.S_ISREG(info.st_mode)
                        or info.st_uid != os.getuid()
                        or stat.S_IMODE(info.st_mode) != 0o600
                        or info.st_nlink != 1
                        or info.st_size > 16_384
                    ):
                        raise ConfigurationError
                    data = os.read(descriptor, 16_385)
                    if len(data) > 16_384:
                        raise ConfigurationError
                    contents = data.decode("utf-8")
                finally:
                    os.close(descriptor)
        finally:
            os.close(directory)
        values = dotenv_values(stream=StringIO(contents), interpolate=False)
        value = os.environ.get("NET_SYPHON_SEARXNG_URL", values.get("NET_SYPHON_SEARXNG_URL"))
        key = os.environ.get(
            "NET_SYPHON_FIRECRAWL_API_KEY", values.get("NET_SYPHON_FIRECRAWL_API_KEY")
        )
        return Settings(searxng_url=value, firecrawl_api_key=key)
    except (OSError, UnicodeError, ValueError, ValidationError, AuditError):
        raise ConfigurationError from None
