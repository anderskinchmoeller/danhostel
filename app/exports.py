"""Local copies of a downloaded price list and its matching v4/v5 comparison."""
from __future__ import annotations

import csv
import io
import json
import os
from pathlib import Path
import tempfile

from .config import BASE_DIR
from .level import locked

OUTPUT_ROOT = BASE_DIR


def ensure_shadow(session, settings, run, recs):
    """Older runs can be backfilled only if their calculation still matches."""
    if run.shadow_json is not None:
        return
    from dataclasses import replace
    from datetime import timedelta
    from sqlalchemy import select
    from . import db, level, service
    from .engine import price_day
    from .skyggedrift import compare_inputs

    if not recs:
        run.shadow_json = "[]"
        return
    days = [r.day for r in recs]
    states = session.scalars(select(db.DayState).where(db.DayState.day.in_(days))).all()
    today = recs[0].day - timedelta(days=recs[0].lead_days)
    params = service.effective_params(session, settings)
    path = Path(params.v4.level_path)
    state = level.load(path if path.is_absolute() else BASE_DIR / path)
    params = replace(params, demand_level=(state.rooms, state.beds))
    inputs = service.build_inputs(states, settings, now=run.finished)
    inputs = service.with_ladder_history(session, inputs, today, run.id, timezone=settings.timezone)
    events = service.load_events(session)
    recalculated = [price_day(item, params, events, today=today) for item in inputs]
    by_day = {r.day: r for r in recalculated}
    for rec in recs:
        actual = by_day.get(rec.day)
        if actual is None or (actual.room_price, actual.bed_price, actual.flex_to_private) != (
                rec.room_price, rec.bed_price, rec.flex_to_private):
            raise ValueError("Kør beregningen igen og godkend priserne: den gamle kørsel "
                             "mangler en sammenligning med samme datagrundlag")
    run.shadow_json = json.dumps(compare_inputs(params, inputs, events, today,
                                                recommendations=recalculated), ensure_ascii=False)


def shadow_csv(run, recs) -> bytes:
    rows = json.loads(run.shadow_json or "[]")
    by_day = {r["dato"]: r for r in rows}
    selected = []
    for rec in recs:
        row = dict(by_day[rec.day.isoformat()])
        row.update(run_id=run.id, status=rec.status)
        selected.append(row)
    buffer = io.StringIO()
    fields = list(rows[0]) if rows else ["dato", "v4_rum", "v5_rum", "v4_seng", "v5_seng"]
    writer = csv.DictWriter(buffer, fieldnames=fields + ["run_id", "status"], delimiter=";")
    writer.writeheader()
    for row in selected:
        writer.writerow({k: str(v).replace(".", ",") if isinstance(v, float) else v
                         for k, v in row.items()})
    return buffer.getvalue().encode("utf-8-sig")


def save_download(run, recs, price_data: bytes, day: str) -> dict:
    """Stage all files before replacing them; retain a copy for each run."""
    comparison = shadow_csv(run, recs)
    files = {
        OUTPUT_ROOT / "dagens_priser" / f"priser_{day}.csv": price_data,
        OUTPUT_ROOT / "dagens_priser" / f"priser_{day}_run-{run.id}.csv": price_data,
        OUTPUT_ROOT / "skygge-v4-v5" / f"skygge_{day}.csv": comparison,
        OUTPUT_ROOT / "skygge-v4-v5" / f"skygge_{day}_run-{run.id}.csv": comparison,
    }
    staged = {}
    replaced = []
    with locked(OUTPUT_ROOT / "data" / "csv-export"):
        previous = {p: p.read_bytes() if p.exists() else None for p in files}
        try:
            for path, content in files.items():
                path.parent.mkdir(parents=True, exist_ok=True)
                with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as fh:
                    staged[path] = Path(fh.name)
                    fh.write(content)
            for path, temp in staged.items():
                os.replace(temp, path)
                replaced.append(path)
        except OSError:
            for path in reversed(replaced):
                if previous[path] is None:
                    path.unlink(missing_ok=True)
                else:
                    path.write_bytes(previous[path])
            raise
        finally:
            for temp in staged.values():
                temp.unlink(missing_ok=True)
    return {"prices": str(next(iter(files))), "shadow": str(list(files)[2])}
