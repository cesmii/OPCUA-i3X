"""Unit tests for the _repo_root upward-walk helper."""

from __future__ import annotations

from pathlib import Path

import pytest

from i3xua.api.routes.admin import _repo_root


def test_repo_root_finds_pyproject_in_current_repo() -> None:
    """Running from inside this repo, _repo_root() returns the directory
    that contains pyproject.toml (the actual repo root)."""
    root = _repo_root()
    assert root is not None
    assert (root / "pyproject.toml").is_file()


def test_repo_root_returns_none_when_no_pyproject(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If the upward walk reaches the filesystem root without finding
    pyproject.toml, return None — the endpoint surfaces this as a 503."""
    # Walk up from a tmp_path subdir that has no pyproject.toml above it.
    leaf = tmp_path / "deep" / "nested" / "module.py"
    leaf.parent.mkdir(parents=True)
    leaf.write_text("# fake")
    # _repo_root takes an optional `start` param so we can test in isolation
    # without monkeypatching __file__.
    assert _repo_root(start=leaf) is None
