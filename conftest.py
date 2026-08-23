"""Make the project root importable in tests (bot, catalog, schema, placement),
and gate the slow SC2 integration tests behind --run-integration."""

import os
import sys

import pytest
from _pytest.config import Config
from _pytest.config.argparsing import Parser
from _pytest.nodes import Item

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def pytest_addoption(parser: Parser) -> None:
    parser.addoption("--run-integration", action="store_true", default=False,
                     help="run integration tests that launch real SC2 (slow)")


def pytest_collection_modifyitems(config: Config, items: list[Item]) -> None:
    if config.getoption("--run-integration"):
        return
    skip = pytest.mark.skip(reason="needs --run-integration (launches SC2)")
    for item in items:
        if "integration" in item.keywords:
            item.add_marker(skip)
