"""External provider contracts are opt-in; normal tests use only local fixtures."""

import pytest


def pytest_addoption(parser):
    parser.addoption("--live", action="store_true", help="Run tests against configured services")


def pytest_collection_modifyitems(config, items):
    if not config.getoption("--live"):
        for item in items:
            if "live" in item.keywords:
                item.add_marker(pytest.mark.skip(reason="External service test requires --live"))
