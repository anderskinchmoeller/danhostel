"""Picasso-adapter (AK Techotel).

VIGTIGT: AK Techotel offentliggør ikke API-dokumentation. Endpoints, felter og
autentifikation nedenfor er derfor PLACEHOLDERE, skrevet så de er nemme at rette
når Techotel har svaret på spørgsmålene i kravspecifikationen:

  1. Findes der et dokumenteret REST- eller webservice-API en tredjepart kan læse
     kapacitet og on-the-books fra, og hvad koster adgangen?
  2. Understøtter Picasso skrivning af priser via API, eller skal priser sættes
     via channel manager (SiteMinder, Sabre, TravelClick, YP Intelligence)?
  3. Er priser modelleret som én BAR med afledte typepriser, eller uafhængigt
     pr. værelsestype?

Ret KUN de tre metoder nedenfor. Resten af servicen kender kun grænsefladen i
adapters/base.py og skal ikke ændres.
"""

from __future__ import annotations

import os
from datetime import date
from typing import Sequence

import httpx

from .base import AdapterUnavailable, CompRow, InventoryRow, PricePush


class PicassoAdapter:
    name = "picasso_api"

    def __init__(
        self,
        base_url: str | None = None,
        api_key: str | None = None,
        property_id: str | None = None,
        timeout: float = 20.0,
    ):
        self.base_url = (base_url or os.getenv("PICASSO_BASE_URL", "")).rstrip("/")
        self.api_key = api_key or os.getenv("PICASSO_API_KEY", "")
        self.property_id = property_id or os.getenv("PICASSO_PROPERTY_ID", "")
        self.timeout = timeout

    # -- interne hjælpere ---------------------------------------------------

    def _client(self) -> httpx.Client:
        if not self.base_url or not self.api_key:
            raise AdapterUnavailable(
                "PICASSO_BASE_URL og PICASSO_API_KEY mangler. "
                "Kør videre på CSV-adapteren indtil Techotel har udleveret adgang."
            )
        return httpx.Client(
            base_url=self.base_url,
            timeout=self.timeout,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Accept": "application/json",
            },
        )

    # -- grænsefladen -------------------------------------------------------

    def fetch_inventory(self, start: date, end: date) -> Sequence[InventoryRow]:
        """FORVENTET svar (ret feltnavne når Techotel har bekræftet):

            [{"date": "2026-09-11", "roomsSold": 24, "bedsSold": 148,
              "roomsBlocked": 4, "bedsBlocked": 0,
              "currentRoomRate": 690, "currentBedRate": 280}, ...]

        Bemærk at BEGGE lagre skal med. Leverer eksporten kun værelser, er den
        halve model blind — spørg efter sengetallene, de findes i Picasso.
        """
        with self._client() as client:
            resp = client.get(
                "/api/v1/availability",  # PLACEHOLDER
                params={
                    "propertyId": self.property_id,
                    "from": start.isoformat(),
                    "to": end.isoformat(),
                },
            )
            resp.raise_for_status()
            payload = resp.json()

        rows = []
        for item in payload:
            rows.append(InventoryRow(
                day=date.fromisoformat(item["date"]),
                rooms_otb=int(item.get("roomsSold", 0)),
                beds_otb=int(item.get("bedsSold", 0)),
                blocked_rooms=int(item.get("roomsBlocked", 0)),
                blocked_beds=int(item.get("bedsBlocked", 0)),
                current_room_price=(float(item["currentRoomRate"]) if item.get("currentRoomRate") else None),
                current_bed_price=(float(item["currentBedRate"]) if item.get("currentBedRate") else None),
            ))
        return rows

    def push_prices(self, prices: Sequence[PricePush]) -> dict:
        """Skriver én værelses-BAR og én sengepris pr. dato.

        Typepriser afledes i Picasso hvis systemet understøtter det — ellers
        sendes room_types med. Skrivning sker i portioner, så en enkelt afvist
        dato ikke vælter resten.
        """
        ok, failed = 0, []
        with self._client() as client:
            for chunk_start in range(0, len(prices), 50):
                chunk = prices[chunk_start:chunk_start + 50]
                body = {
                    "propertyId": self.property_id,
                    "rates": [
                        {
                            "date": p.day.isoformat(),
                            "ratePlan": "BAR",           # PLACEHOLDER
                            "roomAmount": round(p.room_price, 2),
                            "bedAmount": round(p.bed_price, 2),
                            "roomTypes": {k: round(v, 2) for k, v in p.room_types.items()},
                        }
                        for p in chunk
                    ],
                }
                try:
                    resp = client.put("/api/v1/rates", json=body)  # PLACEHOLDER
                    resp.raise_for_status()
                    ok += len(chunk)
                except httpx.HTTPError as exc:
                    failed.extend([p.day.isoformat() for p in chunk])
                    if len(failed) > 10:
                        raise AdapterUnavailable(f"Picasso afviser skrivninger: {exc}") from exc
        return {"ok": ok, "failed": failed}


class LighthouseAdapter:
    """Rate shopping via Lighthouse (tidl. OTA Insight).

    AK Techotel angiver Lighthouse som eksisterende integration, så data kan
    komme ind ad to veje: direkte fra Lighthouse hertil (nedenfor), eller via
    Picasso. Vælg den vej Lighthouse-aftalen dækker.

    Endpoints er placeholdere indtil abonnementet og API-nøglen er på plads.
    Filtrér altid lukkede rater fra — en ikke-tilgængelig pris er ikke en lav pris.
    """

    name = "lighthouse"

    def __init__(self, base_url: str | None = None, api_key: str | None = None,
                 property_id: str | None = None, timeout: float = 20.0):
        self.base_url = (base_url or os.getenv("LIGHTHOUSE_BASE_URL", "")).rstrip("/")
        self.api_key = api_key or os.getenv("LIGHTHOUSE_API_KEY", "")
        self.property_id = property_id or os.getenv("LIGHTHOUSE_PROPERTY_ID", "")
        self.timeout = timeout

    def fetch_comp_prices(self, start: date, end: date) -> Sequence[CompRow]:
        if not self.base_url or not self.api_key:
            raise AdapterUnavailable(
                "LIGHTHOUSE_BASE_URL og LIGHTHOUSE_API_KEY mangler. "
                "Brug manuel upload af konkurrentpriser indtil abonnementet kører."
            )
        with httpx.Client(base_url=self.base_url, timeout=self.timeout,
                          headers={"Authorization": f"Bearer {self.api_key}"}) as client:
            resp = client.get(
                "/v1/rates",  # PLACEHOLDER
                params={
                    "propertyId": self.property_id,
                    "from": start.isoformat(),
                    "to": end.isoformat(),
                    "los": 1,
                    "occupancy": 2,
                },
            )
            resp.raise_for_status()
            payload = resp.json()

        def median(values):
            if not values:
                return None
            values = sorted(values)
            mid = len(values) // 2
            return values[mid] if len(values) % 2 else (values[mid - 1] + values[mid]) / 2

        rows = []
        for item in payload.get("dates", []):
            # Lukkede rater filtreres fra: en pris du ikke kan booke er ikke en
            # lav pris. Det er den klassiske fejl i rate shopping.
            offers = [o for o in item.get("competitors", []) if o.get("available") and o.get("price")]
            room_offers = [float(o["price"]) for o in offers if o.get("unit", "room") == "room"]
            bed_offers = [float(o["price"]) for o in offers if o.get("unit") == "bed"]
            if not offers:
                continue
            rows.append(CompRow(
                day=date.fromisoformat(item["date"]),
                comp_room=median(room_offers),
                comp_bed=median(bed_offers),
                n_properties=len(offers),
            ))
        return rows
