"""Unit tests for the eexgas client package (EEX NGP gas prices)."""
from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import aiohttp
import pytest

from custom_components.ha_nordpool_gas.eexgas import (
    EexGasClient,
    EexGasConnectionError,
    EexGasResponseError,
    GasDayPrice,
    Hub,
    ngp_url,
    parse_ngp_csv,
)
from custom_components.ha_nordpool_gas.eexgas.client import _decode

BERLIN = ZoneInfo("Europe/Berlin")

HEADER = "Gasday;IndexValue (€/MWh);IndexVolume (MWh);Status;Timestamp (CET/CEST)"

# LVA-EST_NGP_15_Mins.csv as served on 2026-10-18 (UTF-8 with BOM, LF line endings).
REAL_FILE = (
    "\ufeff" + HEADER + "\n"
    "17/10/2026;87.661;9684;Final NGP;18/10/2026 22:45\n"
    "18/10/2026;87.957;6233;Temporary NGP;18/10/2026 22:45\n"
    "19/10/2026;88.374;1945;Temporary NGP;18/10/2026 22:45\n"
)

# LTU_NGP_15_Mins.csv as served on 2026-10-18: band and marginal price columns sit
# between the price and Status, and the day after tomorrow is a zero placeholder row.
REAL_LTU_FILE = (
    "\ufeffGasday;LTU NGP (€/MWh);LTU NGP -10% (€/MWh);LTU NGP +10% (€/MWh);IndexVolume (MWh);"
    "LTU Marginal Buy Price (€/MWh);LTU Marginal Sell Price (€/MWh);Status;Timestamp (CET/CEST)\n"
    "17/10/2026;86.856;78.17;95.542;7278;95.542;78.17;Final NGP;18/10/2026 23:00\n"
    "20/10/2026;0;0;0;0;0;0;Temporary NGP;18/10/2026 23:00\n"
)


def _ngp(*rows: str) -> str:
    return "\n".join([HEADER, *rows])


# ---------------------------------------------------------------------------
# parse_ngp_csv
# ---------------------------------------------------------------------------


def test_parse_ngp_csv_real_file():
    rows = parse_ngp_csv(REAL_FILE)
    stamp = datetime(2026, 10, 18, 22, 45, tzinfo=BERLIN)
    assert rows == [
        GasDayPrice(date(2026, 10, 17), 87.661, 9684.0, "Final NGP", stamp),
        GasDayPrice(date(2026, 10, 18), 87.957, 6233.0, "Temporary NGP", stamp),
        GasDayPrice(date(2026, 10, 19), 88.374, 1945.0, "Temporary NGP", stamp),
    ]


def test_parse_ngp_csv_real_ltu_file_finds_columns_by_name():
    stamp = datetime(2026, 10, 18, 23, 0, tzinfo=BERLIN)
    assert parse_ngp_csv(REAL_LTU_FILE) == [
        GasDayPrice(date(2026, 10, 17), 86.856, 7278.0, "Final NGP", stamp),
        GasDayPrice(date(2026, 10, 20), 0.0, 0.0, "Temporary NGP", stamp),
    ]


def test_parse_ngp_csv_header_without_optional_columns():
    (row,) = parse_ngp_csv("Gasday;IndexValue (€/MWh)\n18/10/2026;87.957;6233;Final NGP;18/10/2026 22:45")
    assert row == GasDayPrice(date(2026, 10, 18), 87.957, None, "", None)


def test_parse_ngp_csv_timestamp_is_cet_cest_aware():
    summer, winter = parse_ngp_csv(
        _ngp("18/10/2026;1;1;Final NGP;18/10/2026 22:45", "18/12/2026;1;1;Final NGP;18/12/2026 22:45")
    )
    assert summer.updated_at.utcoffset() == timedelta(hours=2)
    assert winter.updated_at.utcoffset() == timedelta(hours=1)


def test_parse_ngp_csv_comma_decimal():
    (row,) = parse_ngp_csv(_ngp("18/10/2026;87,957;6233,5;Final NGP;18/10/2026 22:45"))
    assert row.price == 87.957
    assert row.volume == 6233.5


def test_parse_ngp_csv_skips_rows_without_gas_day_or_price():
    rows = parse_ngp_csv(
        _ngp(
            "",
            "18/10/2026",
            "2026-10-18;87.9;1;Final NGP;18/10/2026 22:45",
            "18/10/2026;n/a;1;Final NGP;18/10/2026 22:45",
            "19/10/2026;88.4;1;Temporary NGP;18/10/2026 22:45",
        )
    )
    assert [row.gas_day for row in rows] == [date(2026, 10, 19)]


def test_parse_ngp_csv_missing_optional_columns():
    (row,) = parse_ngp_csv(_ngp("18/10/2026;87.957"))
    assert row == GasDayPrice(date(2026, 10, 18), 87.957, None, "", None)


def test_parse_ngp_csv_unreadable_optional_columns_become_none():
    (row,) = parse_ngp_csv(_ngp("18/10/2026;87.957;lots;Final NGP;soon"))
    assert row.volume is None
    assert row.updated_at is None
    assert row.status == "Final NGP"


def test_parse_ngp_csv_keeps_zero_prices_and_duplicates():
    # Choosing between rows is the caller's job; the parser returns the file as published.
    rows = parse_ngp_csv(_ngp("18/10/2026;0;0;Temporary NGP;", "18/10/2026;87.9;1;Temporary NGP;"))
    assert [row.price for row in rows] == [0.0, 87.9]


def test_parse_ngp_csv_header_only():
    assert parse_ngp_csv(HEADER) == []


@pytest.mark.parametrize(
    "text",
    ["", "<!DOCTYPE html><html><body>Maintenance</body></html>", "Date;Price\n18/10/2026;87.9"],
)
def test_parse_ngp_csv_rejects_text_that_is_not_an_ngp_file(text):
    with pytest.raises(EexGasResponseError):
        parse_ngp_csv(text)


# ---------------------------------------------------------------------------
# _decode
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("encoding", ["utf-8-sig", "utf-8", "cp1252"])
def test_decode_handles_each_encoding_eex_has_served(encoding):
    body = REAL_FILE.lstrip("\ufeff").encode(encoding)
    text = _decode(body)
    assert "€" in text
    assert parse_ngp_csv(text) == parse_ngp_csv(REAL_FILE)


# ---------------------------------------------------------------------------
# ngp_url
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("hub", "file_name"),
    [(Hub.LVA_EST, "LVA-EST_NGP_15_Mins.csv"), (Hub.FIN, "FIN_NGP_15_Mins.csv"), (Hub.LTU, "LTU_NGP_15_Mins.csv")],
)
def test_ngp_url(hub, file_name):
    assert ngp_url(hub) == f"https://gasandregistry.eex.com/Gas/NGP/{file_name}"


# ---------------------------------------------------------------------------
# EexGasClient
# ---------------------------------------------------------------------------


class _FakeResponse:
    def __init__(self, body: bytes = b"", status_error: Exception | None = None) -> None:
        self._body = body
        self._status_error = status_error

    def raise_for_status(self) -> None:
        if self._status_error:
            raise self._status_error

    async def read(self) -> bytes:
        return self._body


class _FakeRequest:
    """What session.get() returns: an async context manager that connects on entry."""

    def __init__(self, response: _FakeResponse | None, error: BaseException | None) -> None:
        self._response = response
        self._error = error

    async def __aenter__(self) -> _FakeResponse:
        if self._error:
            raise self._error
        assert self._response is not None
        return self._response

    async def __aexit__(self, *exc_info) -> bool:
        return False


class _FakeSession:
    def __init__(self, response: _FakeResponse | None = None, error: BaseException | None = None) -> None:
        self._response = response
        self._error = error
        self.calls: list[tuple[str, dict]] = []

    def get(self, url: str, **kwargs) -> _FakeRequest:
        self.calls.append((url, kwargs))
        return _FakeRequest(self._response, self._error)


def _get_prices(session: _FakeSession, hub: Hub = Hub.LVA_EST) -> list[GasDayPrice]:
    return asyncio.run(EexGasClient(session).async_get_prices(hub))  # type: ignore[arg-type]


def test_client_fetches_hub_file_with_timeout_and_parses_it():
    session = _FakeSession(_FakeResponse(REAL_FILE.encode("utf-8")))
    rows = _get_prices(session, Hub.FIN)
    assert rows == parse_ngp_csv(REAL_FILE)
    ((url, kwargs),) = session.calls
    assert url == ngp_url(Hub.FIN)
    assert "timeout" in kwargs
    assert "ssl" not in kwargs  # certificate verification stays on


def test_client_wraps_connection_errors():
    error = aiohttp.ClientError("Connection reset by peer")
    with pytest.raises(EexGasConnectionError, match="Connection reset by peer") as excinfo:
        _get_prices(_FakeSession(error=error))
    assert excinfo.value.__cause__ is error
    assert ngp_url(Hub.LVA_EST) in str(excinfo.value)


def test_client_wraps_timeouts_with_a_readable_message():
    with pytest.raises(EexGasConnectionError, match="TimeoutError"):
        _get_prices(_FakeSession(error=TimeoutError()))


def test_client_wraps_http_error_status():
    response = _FakeResponse(status_error=aiohttp.ClientError("503, message='Service Unavailable'"))
    with pytest.raises(EexGasConnectionError, match="503"):
        _get_prices(_FakeSession(response))


def test_client_rejects_a_response_that_is_not_an_ngp_file():
    with pytest.raises(EexGasResponseError):
        _get_prices(_FakeSession(_FakeResponse(b"<html>Maintenance</html>")))
