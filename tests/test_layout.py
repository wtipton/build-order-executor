"""Tier 0 — packaging invariants: the bot must not import anything the image omits.

`.dockerignore` keeps `scripts/` and `tests/` out of the runtime image, so an import of
either from a module the bot loads would pass every test on the host and then fail only
inside the container, at game launch. Nothing else checks this, because on a dev checkout
those directories are always present.

`scripts/__init__.py` states the invariant; this is what enforces it.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent

# Directories excluded from the runtime image by .dockerignore. Importing one of these
# from bot code is the failure this module exists to prevent.
OMITTED_FROM_IMAGE = {"scripts", "tests"}


def _bot_modules() -> list[Path]:
    """The bot's own modules: top-level .py files, which is everything the image runs.

    Deliberately not a hardcoded list — a new module added next to bot.py is covered
    without anybody remembering to update this.
    """
    return sorted(p for p in REPO.glob("*.py") if p.name != "conftest.py")


def _imported_top_level(path: Path) -> set[str]:
    names = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            names.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names.add(node.module.split(".")[0])
    return names


def test_found_the_bot_modules():
    """Guard against the glob silently matching nothing and vacuously passing."""
    names = {p.name for p in _bot_modules()}
    assert {"bot.py", "run.py", "schema.py"} <= names, names


@pytest.mark.parametrize("module", _bot_modules(), ids=lambda p: p.name)
def test_bot_module_imports_nothing_omitted_from_the_image(module):
    offenders = _imported_top_level(module) & OMITTED_FROM_IMAGE
    assert not offenders, (
        f"{module.name} imports {sorted(offenders)}, which .dockerignore keeps out of "
        f"the runtime image — it would fail at game launch inside the container"
    )


def test_measurement_tools_are_not_imported_by_anything_outside_scripts():
    """The other half of the invariant in scripts/__init__.py: the tools may import the
    bot, never the reverse. Covers tests/ too, so the suite doesn't grow a dependency on
    code the image lacks."""
    offenders = []
    for path in sorted(REPO.glob("*.py")) + sorted((REPO / "tests").glob("*.py")):
        if "scripts" in _imported_top_level(path):
            offenders.append(str(path.relative_to(REPO)))
    assert not offenders, f"these import scripts/: {offenders}"
