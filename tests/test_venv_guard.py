from __future__ import annotations

import pytest

from creeper_dripper.cli.main import main, running_from_project_venv


def test_main_exits_when_not_project_venv(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ALLOW_NON_VENV", raising=False)
    monkeypatch.setattr("creeper_dripper.cli.main.running_from_project_venv", lambda *_a, **_k: False)
    with pytest.raises(SystemExit) as exc:
        main(["debug-env"])
    assert exc.value.code == 1


def test_main_continues_with_allow_non_venv(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setenv("ALLOW_NON_VENV", "1")
    monkeypatch.setattr("creeper_dripper.cli.main.running_from_project_venv", lambda *_a, **_k: False)
    code = main(["debug-env"])
    assert code == 0
    err = capsys.readouterr().err
    assert "WARNING: running outside .venv" in err


def test_running_from_project_venv_is_boolean() -> None:
    assert isinstance(running_from_project_venv(), bool)
