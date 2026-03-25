"""Pytest defaults: CLI entry tests invoke `main()` without requiring the project .venv interpreter."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _allow_non_venv_for_tests(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ALLOW_NON_VENV", "1")
