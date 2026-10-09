"""Konfiguration.

Alt der kan justeres uden at røre kode ligger i config/config.yaml.
Hemmeligheder (API-nøgler, kodeord) kommer fra miljøvariabler.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import yaml

from . import demand as demand_module
from . import level as level_module
from .bidprice import V4Config
from .engine import BookingCurve, CurvePoint, Inventory, Params
from .ladder import LadderConfig

BASE_DIR = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = BASE_DIR / "config" / "config.yaml"


def _v4_params(pricing: dict, config_path: Path) -> tuple:
    cfg = _v4(pricing.get("v4"))
    models, level, notes = _v4_state(cfg, config_path.resolve().parent.parent)
    for note in notes:
        V4_NOTES.append(note)
    return cfg, models, level


V4_NOTES: list = []
"""Beskeder fra indlæsningen af version 4.

De vises i /health og i dashboardet. Runtime er v4-only, så manglende eller
ulæseligt modelgrundlag stopper indlæsningen i stedet for at skifte motor.
"""


@dataclass
class Settings:
    params: Params
    horizon_days: int
    auto_publish: bool
    stale_hours: int
    run_cron_hour: int
    run_cron_minute: int
    timezone: str
    pms_adapter: str
    rateshop_adapter: str
    database_url: str
    basic_auth_user: str | None
    basic_auth_password: str | None
    raw: dict


def _curve(raw: list) -> BookingCurve:
    points = [
        CurvePoint(int(p["lead_days_from"]), float(p["weekday"]), float(p["weekend"]))
        for p in raw
    ]
    points.sort(key=lambda p: p.lead_days_from)
    return BookingCurve(points)


def _ladder(raw: dict | None) -> LadderConfig | None:
    """pricing.ladder i config.yaml. Stigen er rammen omkring v4's trinvalg."""
    if not raw or not raw.get("enabled", False):
        return None
    kw = {}
    for key, default in LadderConfig.__dataclass_fields__.items():
        if key not in raw:
            continue
        value = raw[key]
        if key in ("rungs_rooms", "rungs_beds"):
            value = tuple(float(v) for v in value)
        elif key == "sellout_protect":
            value = tuple((float(p), int(o)) for p, o in value)
        elif isinstance(default.default, bool):
            value = bool(value)
        elif isinstance(default.default, int):
            value = int(value)
        elif isinstance(default.default, float):
            value = float(value)
        kw[key] = value
    return LadderConfig(**kw)


def _v4(raw: dict | None) -> V4Config | None:
    """pricing.v4 i config.yaml. Mangler afsnittet, bruges v4-standarderne."""
    if not raw:
        return V4Config(enabled=True)
    kw = {}
    for key, field_ in V4Config.__dataclass_fields__.items():
        if key not in raw:
            continue
        value = raw[key]
        if isinstance(field_.default, bool):
            value = bool(value)
        elif isinstance(field_.default, float):
            value = float(value)
        elif isinstance(field_.default, int):
            value = int(value)
        elif isinstance(field_.default, str):
            value = str(value)
        kw[key] = value
    return V4Config(**kw)


def _v4_state(cfg: V4Config | None, base: Path) -> tuple:
    """Hent efterspørgselsfordelingen og det adaptive niveau fra disk.

    Runtime er v4-only. Mangler modelfilen, eller kan den ikke læses, stoppes
    indlæsningen med en klar fejl, så en natlig kørsel ikke ubemærket bruger en
    ældre motor.
    """
    if cfg is None or not cfg.enabled:
        raise ValueError("pricing.v4.enabled skal være true; projektet kører kun v4")
    path = Path(cfg.model_path)
    if not path.is_absolute():
        path = base / path
    if not path.exists():
        raise FileNotFoundError(
            f"pricing.v4.model_path findes ikke: {cfg.model_path}. "
            "Byg den med: python -m app.cube && python -m app.demand")
    try:
        models = demand_module.load(path)
    except (ValueError, KeyError, OSError) as exc:
        raise ValueError(f"Kunne ikke læse pricing.v4.model_path {cfg.model_path}: {exc}") from exc
    level_path = Path(cfg.level_path)
    if not level_path.is_absolute():
        level_path = base / level_path
    state = level_module.load(level_path)
    notes = []
    if abs(state.rooms - 1.0) > 0.25 or abs(state.beds - 1.0) > 0.25:
        notes.append(f"Efterspørgselsniveauet står på rum {state.rooms:.2f} / "
                     f"senge {state.beds:.2f} — kontrollér at det er et reelt skift")
    return models, (state.rooms, state.beds), notes


def load_settings(path: str | Path | None = None) -> Settings:
    path = Path(path or os.getenv("RMS_CONFIG", DEFAULT_CONFIG))
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))

    inv_raw = raw.get("inventory", {})
    inventory = Inventory(
        private_rooms=int(inv_raw.get("private_rooms", 20)),
        flex_rooms=int(inv_raw.get("flex_rooms", 16)),
        beds_per_flex_room=int(inv_raw.get("beds_per_flex_room", 4)),
        dorm_beds=int(inv_raw.get("dorm_beds", 216)),
        private_beds=inv_raw.get("private_beds"),
        private_room_types=inv_raw.get("private_room_types", {}),
        flex_room_types=inv_raw.get("flex_room_types", {}),
        confirmed=bool(inv_raw.get("confirmed", False)),
    )

    p = raw["pricing"]
    params = Params(
        inventory=inventory,
        bar_base=float(p["bar_base"]),
        bed_base=float(p["bed_base"]),
        season={int(k): float(v) for k, v in p["season"].items()},
        weekday={int(k): float(v) for k, v in p["weekday"].items()},
        weekday_beds={int(k): float(v) for k, v in p["weekday_beds"].items()},
        weekend_days=tuple(int(d) for d in p.get("weekend_days", [4, 5])),
        room_types={k: float(v) for k, v in p["room_types"].items()},
        bed_types={k: float(v) for k, v in (p.get("bed_types") or {}).items()},
        target_occupancy_rooms=float(p["target_occupancy_rooms"]),
        target_occupancy_beds=float(p["target_occupancy_beds"]),
        k_forecast=float(p["k_forecast"]),
        k_forecast_beds=float(p["k_forecast_beds"]),
        pace_min=float(p["pace_min"]),
        pace_max=float(p["pace_max"]),
        k_market=float(p["k_market"]),
        k_market_bed=float(p["k_market_bed"]),
        market_min=float(p["market_min"]),
        market_max=float(p["market_max"]),
        event_max=float(p["event_max"]),
        max_daily_change=float(p["max_daily_change"]),
        variable_cost=float(p["variable_cost"]),
        variable_cost_bed=float(p["variable_cost_bed"]),
        commission=float(p["commission"]),
        price_floor=float(p["price_floor"]),
        price_ceiling=float(p["price_ceiling"]),
        bed_floor=float(p["bed_floor"]),
        bed_ceiling=float(p["bed_ceiling"]),
        rounding=float(p["rounding"]),
        quality_index=float(p.get("quality_index", 1.0)),
        booking_curve=_curve(p["booking_curve"]),
        booking_curve_beds=_curve(p["booking_curve_beds"]),
        ladder=_ladder(p.get("ladder")),
        **dict(zip(("v4", "demand_models", "demand_level"), _v4_params(p, path))),
    )

    if any(t not in params.room_types for t in inventory.private_room_types):
        raise ValueError("Alle lagertyper skal have en prisfaktor")
    if inventory.flex_room_types:
        if any(t not in params.room_types for t in inventory.flex_room_types):
            raise ValueError("Alle flex-rumtyper skal have en prisfaktor")
    elif inventory.flex_rooms and "familie_4" not in params.room_types:
        raise ValueError("Flex-rum kræver prisfaktor familie_4")
    if set(params.bed_types) & set(params.room_types):
        raise ValueError("En kode kan ikke både være rumtype og sengetype")
    ops = raw.get("operations", {})
    return Settings(
        params=params,
        horizon_days=int(ops.get("horizon_days", 120)),
        auto_publish=bool(ops.get("auto_publish", False)),
        stale_hours=int(ops.get("stale_hours", 36)),
        run_cron_hour=int(ops.get("run_cron_hour", 4)),
        run_cron_minute=int(ops.get("run_cron_minute", 15)),
        timezone=ops.get("timezone", "Europe/Copenhagen"),
        pms_adapter=raw.get("adapters", {}).get("pms", "csv"),
        rateshop_adapter=raw.get("adapters", {}).get("rateshop", "manual"),
        database_url=os.getenv("RMS_DATABASE_URL", raw.get("database_url", "sqlite:///data/rms.db")),
        basic_auth_user=os.getenv("RMS_USER"),
        basic_auth_password=os.getenv("RMS_PASSWORD"),
        raw=raw,
    )
