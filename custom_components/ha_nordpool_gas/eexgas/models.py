"""Data models for EEX gas prices."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from enum import StrEnum


class Hub(StrEnum):
    """Gas market areas that have an EEX NGP 15-minute file, valued as in its file name."""

    LVA_EST = "LVA-EST"
    FIN = "FIN"
    LTU = "LTU"


@dataclass(frozen=True, slots=True)
class GasDayPrice:
    """One row of an EEX NGP file, with values as EEX publishes them."""

    gas_day: date
    price: float  # index value, EUR/MWh
    volume: float | None  # index volume, MWh
    status: str  # EEX status text, e.g. "Final NGP" or "Temporary NGP"
    updated_at: datetime | None  # row timestamp, timezone-aware (EEX publishes CET/CEST)
