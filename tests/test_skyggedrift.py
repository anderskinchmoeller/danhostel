"""Skyggedrift skal kunne starte i en ny proces uden webappen."""

import os
from datetime import date, timedelta
from pathlib import Path
import subprocess
import sys

import pytest
import yaml
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app import db
from app.config import DEFAULT_CONFIG


@pytest.mark.parametrize("with_data", [False, True])
def test_skyggedrift_som_selvstaendig_kommando(tmp_path, with_data):
    root = Path(__file__).resolve().parents[1]
    database_url = f"sqlite:///{tmp_path}/shadow.db"
    if with_data:
        engine = create_engine(database_url)
        try:
            db.Base.metadata.create_all(engine)
            with Session(engine) as session:
                session.add(db.DayState(day=date.today() + timedelta(days=1),
                                        rooms_otb=5, beds_otb=10))
                session.commit()
        finally:
            engine.dispose()

    raw = yaml.safe_load(DEFAULT_CONFIG.read_text(encoding="utf-8"))
    raw["pricing"]["ladder"]["enabled"] = True
    raw["pricing"]["v4"]["enabled"] = True
    raw["pricing"]["v4"]["model_path"] = str(root / "config/demand_model.json")
    raw["pricing"]["v4"]["level_path"] = str(tmp_path / "level.json")
    config = tmp_path / "config.yaml"
    config.write_text(yaml.safe_dump(raw), encoding="utf-8")
    result = subprocess.run(
        [sys.executable, "-m", "app.skyggedrift", "--alle", "--dage", "2"],
        cwd=root, env={**os.environ, "RMS_DATABASE_URL": database_url,
                       "RMS_CONFIG": str(config)},
        capture_output=True, text=True, timeout=30,
    )
    assert "Traceback" not in result.stderr, result.stderr
    if with_data:
        assert result.returncode == 0, result.stderr
        assert "1 datoer," in result.stdout
        assert (date.today() + timedelta(days=1)).isoformat() in result.stdout
    else:
        assert result.returncode != 0
        assert "Ingen belægningsdata. Upload dagens fil først." in result.stderr
