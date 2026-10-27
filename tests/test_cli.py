from __future__ import annotations

import pytest

from docboard import __version__, cli


def test_version(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exit_info:
        cli.main(["--version"])
    assert exit_info.value.code == 0
    assert capsys.readouterr().out.strip() == f"docboard {__version__}"


def test_no_command_prints_help_and_fails() -> None:
    assert cli.main([]) == 2
