"""_pg_dsn resolution must widen beyond HERMES_HOME-only .env reads.

Regression for 2026-10-30: a CLI-launched process carried only
HERMES_HOME=<profile dir> and no inherited LCM_PG_DSN. The DSN lives in the
ROOT ~/.hermes/.env, so the old single-file read resolved None, plugin load
raised "LCM_BACKEND=postgres but LCM_PG_DSN is not set", and Hermes silently
fell back to the built-in compressor (flat-50 LCM standard bypassed).
"""
import os
from pathlib import Path

import pytest

from backends import factory


def _write_env(p: Path, dsn: str) -> None:
    p.mkdir(parents=True, exist_ok=True)
    p.joinpath(".env").write_text(f"OTHER=1\nLCM_PG_DSN={dsn}\n")


@pytest.fixture()
def clean_env(monkeypatch):
    monkeypatch.delenv("LCM_PG_DSN", raising=False)
    monkeypatch.delenv("HERMES_HOME", raising=False)
    monkeypatch.setattr(Path, "home", lambda: Path("/home/tester"))


def test_root_env_found_when_home_is_profile_dir(clean_env, tmp_path, monkeypatch):
    root = tmp_path / ".hermes"
    _write_env(root, "dsn-root")
    prof = root / "profiles" / "mike"
    prof.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(prof))
    assert factory._pg_dsn() == "dsn-root"


def test_env_var_wins(clean_env, monkeypatch):
    monkeypatch.setenv("LCM_PG_DSN", "dsn-env")
    assert factory._pg_dsn() == "dsn-env"


def test_no_dsn_no_cluster_fails_loud(clean_env, monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "nowhere"))
    with pytest.raises(RuntimeError, match="LCM_PG_DSN is not set"):
        factory._pg_dsn()
