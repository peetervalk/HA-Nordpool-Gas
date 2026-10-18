"""Async client for EEX Natural Gas Price (NGP) index files."""
from __future__ import annotations

import csv
import io
from collections.abc import Callable
from datetime import datetime
from typing import TypeVar
from zoneinfo import ZoneInfo

import aiohttp

from .exceptions import EexGasConnectionError, EexGasResponseError
from .models import GasDayPrice, Hub

BASE_URL = "https://gasandregistry.eex.com/Gas/NGP"
DEFAULT_TIMEOUT = 30

_HEADER_PREFIX = "Gasday"
_EEX_TZ = ZoneInfo("Europe/Berlin")

_T = TypeVar("_T")


def ngp_url(hub: Hub) -> str:
    """Return the URL of a hub's NGP 15-minute file."""
    return f"{BASE_URL}/{hub.value}_NGP_15_Mins.csv"


def _to_float(value: str) -> float:
    return float(value.strip().replace(",", "."))


def _to_timestamp(value: str) -> datetime:
    return datetime.strptime(value.strip(), "%d/%m/%Y %H:%M").replace(tzinfo=_EEX_TZ)


def _column(header: list[str], prefix: str) -> int | None:
    """Return the index of the first header cell starting with prefix, or None."""
    return next((i for i, name in enumerate(header) if name.strip().startswith(prefix)), None)


def _optional(row: list[str], index: int | None, convert: Callable[[str], _T]) -> _T | None:
    """Return convert(row[index]), or None when the column is missing, empty or unreadable."""
    if index is None or index >= len(row) or not row[index].strip():
        return None
    try:
        return convert(row[index])
    except ValueError:
        return None


def _decode(body: bytes) -> str:
    """Decode an NGP file. EEX has served it both as UTF-8 with a BOM and as Windows-1252."""
    try:
        return body.decode("utf-8-sig")
    except UnicodeDecodeError:
        return body.decode("cp1252", errors="replace")


def parse_ngp_csv(text: str) -> list[GasDayPrice]:
    """Parse an NGP file into one GasDayPrice per readable row, in file order.

    Hubs use two layouts. Most have
    Gasday ; IndexValue (€/MWh) ; IndexVolume (MWh) ; Status ; Timestamp (CET/CEST)
    while LTU adds band and marginal price columns between the price and Status.
    The gas day and price are always the first two columns and are read by position,
    because the euro sign in the price header has been served in different encodings.
    Volume, status and timestamp are found by their (ASCII) header names.
    Rows with an unreadable gas day or price are skipped. An unreadable volume or
    timestamp becomes None.

    Raises EexGasResponseError when the text does not start with the NGP header,
    for example an HTML error page served with status 200.
    """
    reader = csv.reader(io.StringIO(text.lstrip("\ufeff")), delimiter=";")
    header = next(reader, None)
    if not header or not header[0].strip().startswith(_HEADER_PREFIX):
        raise EexGasResponseError(f"Unexpected NGP header: {str(header)[:100]}")
    volume_col = _column(header, "IndexVolume")
    status_col = _column(header, "Status")
    timestamp_col = _column(header, "Timestamp")
    prices: list[GasDayPrice] = []
    for row in reader:
        if len(row) < 2:
            continue
        try:
            gas_day = datetime.strptime(row[0].strip(), "%d/%m/%Y").date()
            price = _to_float(row[1])
        except ValueError:
            continue
        prices.append(
            GasDayPrice(
                gas_day=gas_day,
                price=price,
                volume=_optional(row, volume_col, _to_float),
                status=_optional(row, status_col, str.strip) or "",
                updated_at=_optional(row, timestamp_col, _to_timestamp),
            )
        )
    return prices


class EexGasClient:
    """Fetch EEX NGP gas prices through a caller-provided aiohttp session."""

    def __init__(self, session: aiohttp.ClientSession, timeout: float = DEFAULT_TIMEOUT) -> None:
        self._session = session
        self._timeout = aiohttp.ClientTimeout(total=timeout)

    async def async_get_prices(self, hub: Hub) -> list[GasDayPrice]:
        """Return every row of the hub's NGP file, which covers a few recent gas days.

        Raises EexGasConnectionError when the download fails and EexGasResponseError
        when the response is not an NGP file.
        """
        url = ngp_url(hub)
        try:
            async with self._session.get(url, timeout=self._timeout) as resp:
                resp.raise_for_status()
                body = await resp.read()
        except (aiohttp.ClientError, TimeoutError) as err:
            raise EexGasConnectionError(f"Error fetching {url}: {str(err) or type(err).__name__}") from err
        return parse_ngp_csv(_decode(body))
