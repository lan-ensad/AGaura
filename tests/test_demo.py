"""The P2 demo script keeps working: a full session runs through the services."""

import re
import runpy
from pathlib import Path

import pytest
from sqlalchemy import select

from agaura.config import get_settings
from agaura.db import Database
from agaura.models import Assembly, AssemblyStatus

DEMO = Path(__file__).parents[1] / "scripts" / "demo_session.py"


def test_demo_session_runs(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    main = runpy.run_path(str(DEMO))["main"]
    assert main(["--database", str(tmp_path / "demo.db")]) == 0
    output = capsys.readouterr().out
    assert "40 phones joined, quorum 30" in output
    assert "second ballot from the same phone refused: already_voted" in output
    assert "invalid ballot refused: unknown_option" in output
    assert output.count("sha256 ") == 6  # five polls in session 1, one in session 2
    assert "Condorcet winner:" in output
    assert "chain intact" in output


SEED = Path(__file__).parents[1] / "scripts" / "dev_seed.py"


def test_dev_seed_adds_one_past_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("AGAURA_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("AGAURA_DATABASE_URL", raising=False)
    get_settings.cache_clear()
    main = runpy.run_path(str(SEED))["main"]
    try:
        assert main([]) == 0
        out = capsys.readouterr().out
        assert "Past    :" in out
        assert re.search(r"Delegate: .* -> /manage/\w{6}/delegate#k=[\w-]{43}", out)
        assert main([]) == 0  # again: a new session with drafts, no second past session
        assert "Past    :" not in capsys.readouterr().out
    finally:
        get_settings.cache_clear()
    database = Database(f"sqlite:///{tmp_path / 'agaura.db'}")
    with database.read() as db:
        statuses = sorted(str(status) for status in db.scalars(select(Assembly.status)))
        closed = db.scalar(select(Assembly).where(Assembly.status == AssemblyStatus.CLOSED))
        assert closed is not None
        results = [poll.result for poll in closed.polls]
    database.dispose()
    assert statuses == ["closed", "preparation", "preparation"]
    assert len(results) == 5 and all(result is not None for result in results)
