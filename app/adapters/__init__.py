"""Valg af adaptere ud fra konfigurationen."""

from __future__ import annotations

import os

from .base import AdapterUnavailable, CompRow, InventoryRow, PricePush
from .csv_adapter import CsvPMSAdapter, ManualRateShopAdapter, parse_comp, parse_inventory

__all__ = [
    "AdapterUnavailable", "CompRow", "InventoryRow", "PricePush",
    "parse_comp", "parse_inventory",
    "get_pms_adapter", "get_rateshop_adapter",
]


def get_pms_adapter(name: str):
    """Kun CSV i dag. En API-adapter tilføjes her når der er et API at tale med.

    Et ukendt navn er en tastefejl i config.yaml og skal siges højt — ikke
    falde tilbage på CSV i stilhed, hvor ingen opdager det.
    """
    if name != "csv":
        raise ValueError(
            f"Ukendt PMS-adapter {name!r}. Kun 'csv' findes. "
            "Se afsnittet om adaptere i REFERENCE.md."
        )
    return CsvPMSAdapter(inbox=os.getenv("RMS_CSV_INBOX") or None)


def get_rateshop_adapter(name: str):
    if name != "manual":
        raise ValueError(
            f"Ukendt rate shop-adapter {name!r}. Kun 'manual' findes. "
            "Se afsnittet om adaptere i REFERENCE.md."
        )
    return ManualRateShopAdapter()
