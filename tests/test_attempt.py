"""Tier 0 — the ledgered run wrapper (docker/attempt.py): argument whitelist, run cap.

This is the code that stands between an unprivileged agent and a root process, so the
rejections below are the security boundary, not cosmetics. It runs as root inside the
eval image; everything here is pure logic and needs neither Docker nor SC2.
"""

from __future__ import annotations

import ast
import importlib.util
import json
import sys
from pathlib import Path

import pytest

ATTEMPT_PY = Path(__file__).resolve().parent.parent / "docker" / "attempt.py"


def _load():
    spec = importlib.util.spec_from_file_location("attempt_under_test", ATTEMPT_PY)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def attempt(tmp_path):
    """attempt.py with its filesystem constants redirected into tmp_path."""
    m = _load()
    m.WORKSPACE = tmp_path / "workspace"
    m.LEDGER_ROOT = tmp_path / "ledger"
    m.ATTEMPTS = m.LEDGER_ROOT / "attempts"
    m.INDEX = m.ATTEMPTS / "index.jsonl"
    m.LIMITS = m.LEDGER_ROOT / "limits.json"
    m.WORKSPACE.mkdir()
    return m


@pytest.fixture
def build(attempt):
    """A valid build file inside the fake workspace."""
    path = attempt.WORKSPACE / "b.yaml"
    path.write_text("- {at: asap, do: wait}\n")
    return path


def test_accepts_build_with_default_time_limit(attempt, build):
    assert attempt.parse_args(["--build", str(build)]) == (build, attempt.TIME_LIMIT_DEFAULT)


def test_accepts_equals_form_and_explicit_time_limit(attempt, build):
    assert attempt.parse_args([f"--build={build}", "--time-limit=120"]) == (build, 120)


def test_resolves_symlink_inside_workspace(attempt, build):
    """A symlink that stays inside the workspace is fine — it's escape we reject."""
    link = attempt.WORKSPACE / "link.yaml"
    link.symlink_to(build)
    assert attempt.parse_args(["--build", str(link)]) == (build, attempt.TIME_LIMIT_DEFAULT)


# Each case: argv that must be refused, and a substring the message should mention.
# --replay would be an arbitrary-write primitive as root; --map would let the agent
# measure on different terrain from the one it is scored on; a path outside the
# workspace is a root read primitive via parse errors.
BAD_ARGV = [
    (["--build", "{build}", "--replay", "/etc/passwd"], "unsupported argument"),
    (["--build", "{build}", "--map", "CatalystLE"], "unsupported argument"),
    (["--build", "{build}", "--target", "wine"], "unsupported argument"),
    (["--build", "{build}", "--fullscreen"], "unsupported argument"),
    (["--build"], "needs a value"),
    (["--build", "{build}", "--time-limit"], "needs a value"),
    ([], "--build is required"),
    (["--time-limit", "300"], "--build is required"),
    (["--build", "{build}", "--time-limit", "abc"], "must be an integer"),
    (["--build", "{build}", "--time-limit", "0"], "must be 1..1800"),
    (["--build", "{build}", "--time-limit", "1801"], "must be 1..1800"),
    (["--build", "{build}", "--time-limit", "-5"], "must be 1..1800"),
    (["--build", "/etc/passwd"], "must be a file under"),
    (["--build", "{build}.missing"], "no such build file"),
]


@pytest.mark.parametrize("argv,expected", BAD_ARGV)
def test_rejects(attempt, build, argv, expected, capsys):
    argv = [a.replace("{build}", str(build)) for a in argv]
    with pytest.raises(SystemExit) as exc:
        attempt.parse_args(argv)
    assert exc.value.code == 2
    assert expected in capsys.readouterr().err


def test_rejects_symlink_escaping_workspace(attempt, capsys):
    """The realpath check has to happen before the containment check, or a symlink in
    the workspace reads any file on the box as root."""
    link = attempt.WORKSPACE / "sneaky.yaml"
    link.symlink_to("/etc/passwd")
    with pytest.raises(SystemExit):
        attempt.parse_args(["--build", str(link)])
    assert "must be a file under" in capsys.readouterr().err


def test_rejects_directory_as_build(attempt, capsys):
    with pytest.raises(SystemExit):
        attempt.parse_args(["--build", str(attempt.WORKSPACE)])
    assert "no such build file" in capsys.readouterr().err


def test_allocates_attempts_in_order(attempt):
    for expected in range(3):
        directory, n = attempt.allocate_attempt(cap=5)
        assert n == expected
        assert directory.name == f"{expected:04d}"
        assert directory.is_dir()


def test_cap_refuses_once_spent(attempt, capsys):
    for _ in range(2):
        attempt.allocate_attempt(cap=2)
    with pytest.raises(SystemExit) as exc:
        attempt.allocate_attempt(cap=2)
    assert exc.value.code == 2
    assert "budget exhausted (2/2" in capsys.readouterr().err


def test_zero_cap_is_unlimited(attempt):
    for _ in range(4):
        attempt.allocate_attempt(cap=0)
    assert sum(1 for p in attempt.ATTEMPTS.iterdir() if p.is_dir()) == 4


def test_allocation_skips_names_already_taken(attempt):
    """mkdir is the allocator, so a pre-existing directory must not be overwritten —
    concurrent runs rely on this."""
    attempt.ATTEMPTS.mkdir(parents=True)
    (attempt.ATTEMPTS / "0000").mkdir()
    directory, n = attempt.allocate_attempt(cap=0)
    assert (directory.name, n) == ("0001", 1)


def test_read_limits_missing_file_is_empty(attempt):
    assert attempt.read_limits() == {}


def test_read_limits_parses_file(attempt):
    attempt.LEDGER_ROOT.mkdir()
    attempt.LIMITS.write_text(json.dumps({"max_attempts": 7, "map": "CatalystLE"}))
    assert attempt.read_limits() == {"max_attempts": 7, "map": "CatalystLE"}


def test_read_limits_rejects_malformed(attempt, capsys):
    attempt.LEDGER_ROOT.mkdir()
    attempt.LIMITS.write_text("{not json")
    with pytest.raises(SystemExit):
        attempt.read_limits()
    assert "malformed" in capsys.readouterr().err


def test_imports_stdlib_only():
    """run_build.c invokes this module with `python -I`, which keeps the script's own
    directory off sys.path. An import of anything from /opt/executor would therefore
    fail at runtime, and only here would catch it."""
    tree = ast.parse(ATTEMPT_PY.read_text())
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            imported.add(node.module.split(".")[0])
    assert imported <= sys.stdlib_module_names, imported - sys.stdlib_module_names
