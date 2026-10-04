import pytest

from qqresults import cli


def test_version(capsys):
    with pytest.raises(SystemExit) as e:
        cli.main(["--version"])
    assert e.value.code == 0
    assert "qqresults" in capsys.readouterr().out
