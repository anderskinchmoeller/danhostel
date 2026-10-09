"""Lighthouse Market Insight helpers.

The files exported from Lighthouse are data inputs, not pricing rules. This
module turns them into a small, explicit summary that can be reviewed and then
fed into documentation or future model calibration.
"""

from __future__ import annotations

import argparse
import csv
import json
import statistics
import zipfile
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Iterable


LEVEL_SCORE = {
    "very low": -1.0,
    "low": -0.6,
    "normal": 0.0,
    "elevated": 0.6,
    "high": 1.0,
    "very high": 1.0,
    "lower": -0.6,
    "higher": 0.6,
}

COMPS_SET = {
    "primary": [
        {"name": "Roberta's Society Aarhus", "distance_km": 0.45, "rating": None},
    ],
    "secondary": [
        {"name": "Cabinn Aarhus", "distance_km": 0.18, "rating": 2},
        {"name": "Wakeup - Aarhus", "distance_km": 0.84, "rating": 2},
    ],
}

ROOM_MAPPING = {
    "danhostel_aarhus_city": {
        "budget_room": ["Economy double room"],
        "standard_room": ["Double/2 person with private bathroom"],
        "single_room": ["Single room"],
        "family_room": ["Family room - with private bathroom"],
        "shared_bathroom": [
            "Private double room with shared bathroom",
            "Private family room with shared bathroom",
        ],
    },
    "robertas_society_aarhus": {
        "standard_room": ["Double or twin room", "Double room"],
        "premium_room": ["Deluxe room"],
        "shared_bathroom": ["Twin room with shared toilet"],
    },
    "cabinn_aarhus": {
        "budget_room": ["Economy room"],
        "standard_room": ["Captain room", "Commodore room", "Queen room", "Standard room"],
        "apartment": ["Studio apartment"],
    },
    "wakeup_aarhus": {
        "standard_room": ["Heaven double room"],
        "family_room": ["Family room"],
    },
}


def _num(value) -> float | None:
    if value is None:
        return None
    text = str(value).strip().replace("\xa0", "").replace("kr.", "").replace("kr", "")
    if not text:
        return None
    if "," in text and "." in text:
        text = text.replace(".", "").replace(",", ".")
    elif "," in text:
        text = text.replace(",", ".")
    return float(text)


def _pct(value: float | None) -> float | None:
    return None if value is None else round(value, 4)


def _money(value: float | None) -> float | None:
    return None if value is None else round(value, 2)


def _excel_date(value: str) -> str:
    serial = int(float(value))
    return (date(1899, 12, 30) + timedelta(days=serial)).isoformat()


def read_csv_rows(path: Path) -> list[dict]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle, delimiter=";"))


def counts(rows: Iterable[dict], field: str) -> dict:
    return dict(Counter(row.get(field, "") for row in rows if row.get(field, "") != ""))


def level_average(rows: Iterable[dict], field: str) -> float | None:
    values = [LEVEL_SCORE[row[field].strip().lower()] for row in rows
              if row.get(field, "").strip().lower() in LEVEL_SCORE]
    return round(statistics.mean(values), 3) if values else None


def summarize_model_vs_lighthouse(rows: list[dict]) -> dict:
    diffs = [_num(row.get("room_diff_lighthouse_minus_model")) for row in rows]
    pcts = [_num(row.get("room_diff_pct_lighthouse_vs_model")) for row in rows]
    prices = [_num(row.get("lighthouse_my_price")) for row in rows]
    diffs = [value for value in diffs if value is not None]
    pcts = [value for value in pcts if value is not None]
    prices = [value for value in prices if value is not None]

    by_compset: dict[str, list[float]] = defaultdict(list)
    by_demand: dict[str, list[float]] = defaultdict(list)
    for row in rows:
        pct = _num(row.get("room_diff_pct_lighthouse_vs_model"))
        if pct is None:
            continue
        by_compset[row.get("lighthouse_compset_price_level", "")].append(pct)
        by_demand[row.get("lighthouse_demand_level", "")].append(pct)

    return {
        "period": {
            "from": min(row["dato"] for row in rows),
            "to": max(row["dato"] for row in rows),
            "n_dates": len(rows),
        },
        "model_gap_to_lighthouse_room_price": {
            "average_dkk": _money(statistics.mean(diffs)),
            "median_dkk": _money(statistics.median(diffs)),
            "min_dkk": _money(min(diffs)),
            "max_dkk": _money(max(diffs)),
            "average_pct": _pct(statistics.mean(pcts)),
            "median_pct": _pct(statistics.median(pcts)),
            "dates_model_below_lighthouse": sum(value > 0 for value in diffs),
            "dates_model_above_lighthouse": sum(value < 0 for value in diffs),
            "dates_more_than_10_pct_below": sum(value > 0.10 for value in pcts),
            "dates_more_than_20_pct_below": sum(value > 0.20 for value in pcts),
        },
        "lighthouse_my_price": {
            "average_dkk": _money(statistics.mean(prices)),
            "median_dkk": _money(statistics.median(prices)),
            "min_dkk": _money(min(prices)),
            "max_dkk": _money(max(prices)),
        },
        "level_counts": {
            "my_price": counts(rows, "lighthouse_my_price_level"),
            "compset_price": counts(rows, "lighthouse_compset_price_level"),
            "demand": counts(rows, "lighthouse_demand_level"),
            "flight": counts(rows, "lighthouse_flight_level"),
            "hotel": counts(rows, "lighthouse_hotel_level"),
            "events_holidays": counts(rows, "lighthouse_events_holidays"),
        },
        "level_scores": {
            "compset_price": level_average(rows, "lighthouse_compset_price_level"),
            "demand": level_average(rows, "lighthouse_demand_level"),
            "flight": level_average(rows, "lighthouse_flight_level"),
            "hotel": level_average(rows, "lighthouse_hotel_level"),
        },
        "gap_by_compset_price_level": {
            key: {
                "n_dates": len(values),
                "average_pct": _pct(statistics.mean(values)),
                "median_pct": _pct(statistics.median(values)),
            }
            for key, values in sorted(by_compset.items()) if key
        },
        "gap_by_demand_level": {
            key: {
                "n_dates": len(values),
                "average_pct": _pct(statistics.mean(values)),
                "median_pct": _pct(statistics.median(values)),
            }
            for key, values in sorted(by_demand.items()) if key
        },
    }


def _xlsx_values(path: Path) -> dict[str, list[list[str | float]]]:
    ns = {
        "main": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
        "rel": "http://schemas.openxmlformats.org/package/2006/relationships",
    }
    with zipfile.ZipFile(path) as archive:
        shared = []
        if "xl/sharedStrings.xml" in archive.namelist():
            root = ET.fromstring(archive.read("xl/sharedStrings.xml"))
            for item in root.findall("main:si", ns):
                shared.append("".join(t.text or "" for t in item.findall(".//main:t", ns)))

        rels = ET.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
        targets = {rel.attrib["Id"]: rel.attrib["Target"] for rel in rels}
        workbook = ET.fromstring(archive.read("xl/workbook.xml"))
        out = {}
        for sheet in workbook.find("main:sheets", ns):
            name = sheet.attrib["name"]
            rid = sheet.attrib["{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"]
            target = "xl/" + targets[rid].lstrip("/")
            root = ET.fromstring(archive.read(target))
            cells = {}
            max_row = max_col = 0
            for cell in root.findall(".//main:c", ns):
                ref = cell.attrib["r"]
                value = cell.find("main:v", ns)
                if value is None:
                    continue
                col_text = "".join(ch for ch in ref if ch.isalpha())
                row = int("".join(ch for ch in ref if ch.isdigit()))
                col = 0
                for ch in col_text:
                    col = col * 26 + ord(ch) - ord("A") + 1
                raw = value.text
                if cell.attrib.get("t") == "s":
                    raw = shared[int(raw)]
                else:
                    try:
                        raw = float(raw)
                    except (TypeError, ValueError):
                        pass
                cells[(row, col)] = raw
                max_row, max_col = max(max_row, row), max(max_col, col)
            out[name] = [
                [cells.get((row, col), "") for col in range(1, max_col + 1)]
                for row in range(1, max_row + 1)
            ]
        return out


def _table_from_sheet(rows: list[list[str | float]], header_row_index: int) -> list[dict]:
    headers = [str(value).strip() for value in rows[header_row_index]]
    out = []
    for row in rows[header_row_index + 1:]:
        record = {headers[i]: row[i] for i in range(min(len(headers), len(row))) if headers[i]}
        if any(value != "" for value in record.values()):
            out.append(record)
    return out


def summarize_market_workbook(path: Path) -> dict:
    sheets = _xlsx_values(path)
    daily = _table_from_sheet(sheets["Daily details"], 4)
    geo = _table_from_sheet(sheets["Geo breakdown"], 4)
    stay = _table_from_sheet(sheets["Stay pattern breakdown"], 4)

    unavailable = [_num(row.get("Unavailable hotels")) for row in daily]
    unavailable = [value for value in unavailable if value is not None]
    demand_counts = dict(Counter(str(row.get("Demand level")) for row in daily if row.get("Demand level")))

    def top_geo(prefix: str) -> list[dict]:
        code_col = f"Top 10 countries searching for {prefix}"
        mix_col = f"Top 10 countries searching for {prefix} % mix"
        los_col = f"Top 10 countries searching for {prefix} avg. LOS"
        return [
            {
                "country": row.get(code_col),
                "mix": _pct(_num(row.get(mix_col))),
                "average_los": round(_num(row.get(los_col)) or 0, 2),
            }
            for row in geo if row.get(code_col)
        ]

    return {
        "market_report_period": {
            "from": _excel_date(str(sheets["Daily details"][1][6])),
            "to": _excel_date(str(sheets["Daily details"][1][9])),
            "n_weeks": len(daily),
        },
        "weekly_demand_level_counts": demand_counts,
        "average_unavailable_hotels": _pct(statistics.mean(unavailable)) if unavailable else None,
        "events_holidays_by_week": {
            _excel_date(str(row["Date"])): int(_num(row.get("Nr. of events and holidays")) or 0)
            for row in daily if row.get("Date")
        },
        "top_origin_countries": {
            "flights": top_geo("flights"),
            "hotels": top_geo("hotels"),
        },
        "stay_pattern": [
            {
                "bucket": row.get("LOS bucket"),
                "flights": _pct(_num(row.get("LOS breakdown flights"))),
                "hotels": _pct(_num(row.get("LOS breakdown hotels"))),
            }
            for row in stay if row.get("LOS bucket")
        ],
    }


def build_summary(model_csv: Path, workbook: Path | None = None) -> dict:
    summary = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "sources": {
            "model_vs_lighthouse": str(model_csv),
            "market_workbook": str(workbook) if workbook else None,
            "screenshots": [
                "compset membership and Booking.com room mapping, 2026-10-08",
            ],
        },
        "compset": COMPS_SET,
        "booking_room_mapping": ROOM_MAPPING,
        "model_vs_lighthouse": summarize_model_vs_lighthouse(read_csv_rows(model_csv)),
    }
    if workbook:
        summary["market_insight_workbook"] = summarize_market_workbook(workbook)
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Summarise Lighthouse exports")
    parser.add_argument("model_csv", type=Path)
    parser.add_argument("--workbook", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)

    summary = build_summary(args.model_csv, args.workbook)
    text = json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text, encoding="utf-8")
    else:
        print(text, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
