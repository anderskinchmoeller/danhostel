"""Valg af adaptere ud fra konfigurationen."""

from __future__ import annotations

import os

from .base import AdapterUnavailable, CompRow, InventoryRow, PricePush
from .csv_adapter import CsvPMSAdapter, ManualRateShopAdapter, parse_comp, parse_inventory
from .picasso_api import LighthouseAdapter, PicassoAdapter

__all__ = [
    "AdapterUnavailable", "CompRow", "InventoryRow", "PricePush",
    "parse_comp", "parse_inventory",
    "get_pms_adapter", "get_rateshop_adapter",
]


def get_pms_adapter(name: str):
    if name == "picasso_api":
        return PicassoAdapter()
    return CsvPMSAdapter(inbox=os.getenv("RMS_CSV_INBOX") or None)


def get_rateshop_adapter(name: str):
    if name == "lighthouse":
        return LighthouseAdapter()
    return ManualRateShopAdapter()
