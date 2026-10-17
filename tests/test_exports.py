"""Downloaded price lists and comparisons must remain matched and recoverable."""
import json
from datetime import date
from types import SimpleNamespace

import pytest

from app import exports


def example_run(run_id):
    return SimpleNamespace(id=run_id, shadow_json=json.dumps([{
        "dato": "2026-10-16", "v4_rum": 680.0, "v5_rum": 640.0,
        "v4_seng": 320.0, "v5_seng": 280.0}]))


def test_later_run_preserves_the_earlier_run_and_exports_only_selected_dates(tmp_path, monkeypatch):
    monkeypatch.setattr(exports, "OUTPUT_ROOT", tmp_path)
    rec = SimpleNamespace(day=date(2026, 10, 16), status="approved")
    exports.save_download(example_run(1), [rec], b"first", "2026-10-16")
    first_shadow = (tmp_path / "skygge-v4-v5/skygge_2026-10-16_run-1.csv").read_bytes()
    exports.save_download(example_run(2), [rec], b"second", "2026-10-16")
    assert (tmp_path / "dagens_priser/priser_2026-10-16.csv").read_bytes() == b"second"
    assert (tmp_path / "dagens_priser/priser_2026-10-16_run-1.csv").read_bytes() == b"first"
    assert (tmp_path / "skygge-v4-v5/skygge_2026-10-16_run-1.csv").read_bytes() == first_shadow
    run = example_run(3)
    rows = json.loads(run.shadow_json)
    rows.append({**rows[0], "dato": "2026-10-17"})
    run.shadow_json = json.dumps(rows)
    shadow = exports.shadow_csv(run, [rec]).decode("utf-8-sig")
    assert "2026-10-16" in shadow and "2026-10-17" not in shadow


def test_failed_write_keeps_existing_pair_and_removes_partial_archives(tmp_path, monkeypatch):
    monkeypatch.setattr(exports, "OUTPUT_ROOT", tmp_path)
    rec = SimpleNamespace(day=date(2026, 10, 16), status="approved")
    exports.save_download(example_run(1), [rec], b"original", "2026-10-16")
    before = {p: p.read_bytes() for p in tmp_path.rglob("*.csv")}
    original_replace = exports.os.replace
    calls = 0

    def fail_on_second_replace(src, dst):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("Disk full")
        return original_replace(src, dst)

    monkeypatch.setattr(exports.os, "replace", fail_on_second_replace)
    with pytest.raises(OSError, match="Disk full"):
        exports.save_download(example_run(2), [rec], b"new", "2026-10-16")
    assert {p: p.read_bytes() for p in tmp_path.rglob("*.csv")} == before
    assert all(p.suffix == ".csv" for folder in ("dagens_priser", "skygge-v4-v5")
               for p in (tmp_path / folder).iterdir())
