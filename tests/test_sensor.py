"""Unit tests for the pure (layer-1) functions in sensor.py."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo

import pytest

from custom_components.ha_nordpool_gas.const import (
    CONF_TRANSFER_DAY,
    CONF_TRANSFER_DAY_END,
    CONF_TRANSFER_DAY_START,
    CONF_TRANSFER_FIXED,
    CONF_TRANSFER_MODE,
    CONF_TRANSFER_NIGHT,
    CONF_TRANSFER_WEEKENDS_NIGHT,
    TRANSFER_MODE_DAY_NIGHT,
    TRANSFER_MODE_FIXED,
    TRANSFER_MODE_NONE,
)
from custom_components.ha_nordpool_gas.sensor import (
    _build_elering_url,
    _build_hourly_averages,
    _make_transfer_fn,
    _parse_electricity_csv,
    _parse_gas_csv,
    _rows_to_list,
)

TALLINN = ZoneInfo("Europe/Tallinn")


# ---------------------------------------------------------------------------
# CSV helpers
# ---------------------------------------------------------------------------


def _elec_csv(*rows: tuple[int, float]) -> str:
    """Minimal Elering-style CSV: epoch, ignored local-time column, price."""
    lines = ["Ajatempel (UTC);LocalTime;ee (EUR/MWh)"]
    for epoch, price in rows:
        lines.append(f"{epoch};ignored;{str(price).replace('.', ',')}")
    return "\n".join(lines)


def _gas_csv(*rows: tuple[str, float]) -> str:
    """Minimal EEX-style gas CSV: date string (dd/mm/yyyy), price."""
    lines = ["Gasday;IndexValue (?/MWh);IndexVolume;Status;Timestamp"]
    for date_str, price in rows:
        lines.append(f"{date_str};{str(price).replace('.', ',')};1000;Final;2024-01-01T00:00:00")
    return "\n".join(lines)


def _epoch(dt_aware: datetime) -> int:
    return int(dt_aware.timestamp())


# ---------------------------------------------------------------------------
# _build_elering_url
# ---------------------------------------------------------------------------


def test_build_elering_url_pads_one_day_each_side():
    url = _build_elering_url(
        "https://example.com/csv", "ee", date(2024, 6, 15), date(2024, 6, 16)
    )
    params = parse_qs(urlparse(url).query)
    assert params["fields"] == ["ee"]
    # start = today - 1
    assert params["start"][0].startswith("2024-06-14")
    # end = tomorrow + 1
    assert params["end"][0].startswith("2024-06-17")


def test_build_elering_url_preserves_base():
    url = _build_elering_url(
        "https://dashboard.elering.ee/api/nps/price/csv",
        "lv",
        date(2024, 1, 1),
        date(2024, 1, 2),
    )
    parsed = urlparse(url)
    assert parsed.scheme == "https"
    assert parsed.netloc == "dashboard.elering.ee"
    assert parsed.path == "/api/nps/price/csv"
    assert parse_qs(parsed.query)["fields"] == ["lv"]


# ---------------------------------------------------------------------------
# _make_transfer_fn
# ---------------------------------------------------------------------------


def test_transfer_fn_none_always_zero():
    fn = _make_transfer_fn({CONF_TRANSFER_MODE: TRANSFER_MODE_NONE})
    assert fn(datetime(2024, 6, 15, 0, 0)) == 0.0
    assert fn(datetime(2024, 6, 15, 14, 0)) == 0.0


def test_transfer_fn_fixed_constant():
    fn = _make_transfer_fn(
        {CONF_TRANSFER_MODE: TRANSFER_MODE_FIXED, CONF_TRANSFER_FIXED: 10.5}
    )
    assert fn(datetime(2024, 6, 15, 0, 0)) == 10.5
    assert fn(datetime(2024, 6, 15, 23, 59)) == 10.5


def test_transfer_fn_day_night_weekday_day_rate():
    fn = _make_transfer_fn(
        {
            CONF_TRANSFER_MODE: TRANSFER_MODE_DAY_NIGHT,
            CONF_TRANSFER_DAY: 8.0,
            CONF_TRANSFER_NIGHT: 3.0,
            CONF_TRANSFER_DAY_START: 7,
            CONF_TRANSFER_DAY_END: 22,
            CONF_TRANSFER_WEEKENDS_NIGHT: True,
        }
    )
    # 2024-06-17 is Monday; 07:00 is first day hour, 21:59 is last
    assert fn(datetime(2024, 6, 17, 7, 0)) == 8.0
    assert fn(datetime(2024, 6, 17, 21, 59)) == 8.0


def test_transfer_fn_day_night_weekday_night_rate():
    fn = _make_transfer_fn(
        {
            CONF_TRANSFER_MODE: TRANSFER_MODE_DAY_NIGHT,
            CONF_TRANSFER_DAY: 8.0,
            CONF_TRANSFER_NIGHT: 3.0,
            CONF_TRANSFER_DAY_START: 7,
            CONF_TRANSFER_DAY_END: 22,
            CONF_TRANSFER_WEEKENDS_NIGHT: True,
        }
    )
    # 22:00 is first night hour; 06:59 is last night hour
    assert fn(datetime(2024, 6, 17, 22, 0)) == 3.0
    assert fn(datetime(2024, 6, 17, 6, 59)) == 3.0


def test_transfer_fn_day_night_weekend_forced_night():
    fn = _make_transfer_fn(
        {
            CONF_TRANSFER_MODE: TRANSFER_MODE_DAY_NIGHT,
            CONF_TRANSFER_DAY: 8.0,
            CONF_TRANSFER_NIGHT: 3.0,
            CONF_TRANSFER_DAY_START: 7,
            CONF_TRANSFER_DAY_END: 22,
            CONF_TRANSFER_WEEKENDS_NIGHT: True,
        }
    )
    # 2024-06-15 is Saturday; weekends_night=True → night even during day hours
    assert fn(datetime(2024, 6, 15, 12, 0)) == 3.0


def test_transfer_fn_day_night_weekend_not_forced():
    fn = _make_transfer_fn(
        {
            CONF_TRANSFER_MODE: TRANSFER_MODE_DAY_NIGHT,
            CONF_TRANSFER_DAY: 8.0,
            CONF_TRANSFER_NIGHT: 3.0,
            CONF_TRANSFER_DAY_START: 7,
            CONF_TRANSFER_DAY_END: 22,
            CONF_TRANSFER_WEEKENDS_NIGHT: False,
        }
    )
    # weekends_night=False → Saturday at noon uses day rate
    assert fn(datetime(2024, 6, 15, 12, 0)) == 8.0
    # weekends_night=False → Saturday at night still uses night rate
    assert fn(datetime(2024, 6, 15, 23, 0)) == 3.0


# ---------------------------------------------------------------------------
# _parse_gas_csv
# ---------------------------------------------------------------------------


def test_parse_gas_csv_today_and_tomorrow():
    text = _gas_csv(("15/06/2024", 45.5), ("16/06/2024", 46.0))
    today, tomorrow = _parse_gas_csv(text, "15/06/2024", "16/06/2024")
    assert today == 45.5
    assert tomorrow == 46.0


def test_parse_gas_csv_zero_price_skipped():
    # First row is zero → skip; second row is the real price
    text = _gas_csv(("15/06/2024", 0.0), ("15/06/2024", 45.5))
    today, _ = _parse_gas_csv(text, "15/06/2024", "16/06/2024")
    assert today == 45.5


def test_parse_gas_csv_tomorrow_absent():
    text = _gas_csv(("15/06/2024", 45.5))
    today, tomorrow = _parse_gas_csv(text, "15/06/2024", "16/06/2024")
    assert today == 45.5
    assert tomorrow is None


def test_parse_gas_csv_first_nonzero_wins():
    text = _gas_csv(("15/06/2024", 45.5), ("15/06/2024", 99.0))
    today, _ = _parse_gas_csv(text, "15/06/2024", "16/06/2024")
    assert today == 45.5


def test_parse_gas_csv_irrelevant_dates_ignored():
    text = _gas_csv(("13/06/2024", 40.0), ("15/06/2024", 45.5))
    today, tomorrow = _parse_gas_csv(text, "15/06/2024", "16/06/2024")
    assert today == 45.5
    assert tomorrow is None


def test_parse_gas_csv_empty_text():
    today, tomorrow = _parse_gas_csv("", "15/06/2024", "16/06/2024")
    assert today is None
    assert tomorrow is None


# ---------------------------------------------------------------------------
# _parse_electricity_csv
# ---------------------------------------------------------------------------


def test_parse_electricity_csv_vat_applied():
    today = date(2024, 6, 15)
    epoch = _epoch(datetime(2024, 6, 15, 12, 0, tzinfo=TALLINN))
    text = _elec_csv((epoch, 50.0))
    no_transfer = _make_transfer_fn({CONF_TRANSFER_MODE: TRANSFER_MODE_NONE})

    today_rows, _ = _parse_electricity_csv(text, 22.0, no_transfer, today, today + timedelta(days=1))

    assert len(today_rows) == 1
    # 50.0 * 1.22 = 61.0
    assert today_rows[0][1] == pytest.approx(61.0)


def test_parse_electricity_csv_transfer_fee_applied():
    today = date(2024, 6, 15)
    epoch = _epoch(datetime(2024, 6, 15, 14, 0, tzinfo=TALLINN))
    text = _elec_csv((epoch, 50.0))
    fixed_transfer = _make_transfer_fn(
        {CONF_TRANSFER_MODE: TRANSFER_MODE_FIXED, CONF_TRANSFER_FIXED: 5.0}
    )

    today_rows, _ = _parse_electricity_csv(text, 22.0, fixed_transfer, today, today + timedelta(days=1))

    # (50 + 5) * 1.22 = 67.1
    assert today_rows[0][1] == pytest.approx(67.1)


def test_parse_electricity_csv_today_and_tomorrow_split():
    today = date(2024, 6, 15)
    tomorrow = date(2024, 6, 16)
    epoch_today = _epoch(datetime(2024, 6, 15, 12, 0, tzinfo=TALLINN))
    epoch_tomorrow = _epoch(datetime(2024, 6, 16, 12, 0, tzinfo=TALLINN))
    text = _elec_csv((epoch_today, 50.0), (epoch_tomorrow, 60.0))
    no_transfer = _make_transfer_fn({CONF_TRANSFER_MODE: TRANSFER_MODE_NONE})

    today_rows, tomorrow_rows = _parse_electricity_csv(text, 0.0, no_transfer, today, tomorrow)

    assert len(today_rows) == 1
    assert len(tomorrow_rows) == 1


def test_parse_electricity_csv_midnight_row_classified_as_local_date():
    """00:15 local time is 21:15 UTC the previous calendar day.
    The row must be classified by local date, not UTC date."""
    today = date(2024, 6, 15)
    # 00:15 EEST on 2024-06-15 = 21:15 UTC on 2024-06-14
    epoch = _epoch(datetime(2024, 6, 15, 0, 15, tzinfo=TALLINN))
    text = _elec_csv((epoch, 50.0))
    no_transfer = _make_transfer_fn({CONF_TRANSFER_MODE: TRANSFER_MODE_NONE})

    today_rows, tomorrow_rows = _parse_electricity_csv(
        text, 0.0, no_transfer, today, today + timedelta(days=1)
    )

    assert len(today_rows) == 1, "00:15 local should be today, not skipped as UTC-previous-day"
    assert len(tomorrow_rows) == 0


def test_parse_electricity_csv_rows_are_timezone_aware():
    """Returned datetimes must be timezone-aware (not naive) for correct timestamp serialisation."""
    today = date(2024, 6, 15)
    epoch = _epoch(datetime(2024, 6, 15, 12, 0, tzinfo=TALLINN))
    text = _elec_csv((epoch, 50.0))
    no_transfer = _make_transfer_fn({CONF_TRANSFER_MODE: TRANSFER_MODE_NONE})

    today_rows, _ = _parse_electricity_csv(text, 0.0, no_transfer, today, today + timedelta(days=1))

    dt, _ = today_rows[0]
    assert dt.tzinfo is not None
    assert dt.hour == 12


def test_parse_electricity_csv_out_of_range_rows_discarded():
    today = date(2024, 6, 15)
    tomorrow = date(2024, 6, 16)
    epoch_past = _epoch(datetime(2024, 6, 14, 12, 0, tzinfo=TALLINN))
    epoch_future = _epoch(datetime(2024, 6, 17, 12, 0, tzinfo=TALLINN))
    text = _elec_csv((epoch_past, 40.0), (epoch_future, 80.0))
    no_transfer = _make_transfer_fn({CONF_TRANSFER_MODE: TRANSFER_MODE_NONE})

    today_rows, tomorrow_rows = _parse_electricity_csv(text, 0.0, no_transfer, today, tomorrow)

    assert len(today_rows) == 0
    assert len(tomorrow_rows) == 0


# ---------------------------------------------------------------------------
# _build_hourly_averages
# ---------------------------------------------------------------------------


def test_build_hourly_averages_four_slots():
    tz = ZoneInfo("UTC")
    rows = [
        (datetime(2024, 6, 15, 10, 0, tzinfo=tz), 40.0),
        (datetime(2024, 6, 15, 10, 15, tzinfo=tz), 50.0),
        (datetime(2024, 6, 15, 10, 30, tzinfo=tz), 60.0),
        (datetime(2024, 6, 15, 10, 45, tzinfo=tz), 50.0),
    ]
    result = _build_hourly_averages(rows)
    assert result[10] == pytest.approx(50.0)


def test_build_hourly_averages_multiple_hours():
    tz = ZoneInfo("UTC")
    rows = [
        (datetime(2024, 6, 15, 10, 0, tzinfo=tz), 40.0),
        (datetime(2024, 6, 15, 11, 0, tzinfo=tz), 80.0),
        (datetime(2024, 6, 15, 11, 15, tzinfo=tz), 60.0),
    ]
    result = _build_hourly_averages(rows)
    assert result[10] == pytest.approx(40.0)
    assert result[11] == pytest.approx(70.0)


def test_build_hourly_averages_empty():
    assert _build_hourly_averages([]) == {}


# ---------------------------------------------------------------------------
# _rows_to_list
# ---------------------------------------------------------------------------


def test_rows_to_list_produces_correct_utc_epoch():
    """Aware datetime must yield a correct UTC Unix timestamp, not an OS-local one."""
    # 12:00 EEST = 09:00 UTC
    dt = datetime(2024, 6, 15, 12, 0, tzinfo=TALLINN)
    result = _rows_to_list([(dt, 55.0)])

    stored_epoch = result[0][0]
    reconstructed = datetime.fromtimestamp(stored_epoch, tz=timezone.utc)
    assert reconstructed.hour == 9  # must be UTC hour, not local hour
    assert result[0][1] == 55.0


def test_rows_to_list_preserves_price():
    dt = datetime(2024, 6, 15, 8, 0, tzinfo=ZoneInfo("UTC"))
    result = _rows_to_list([(dt, 123.45)])
    assert result[0][1] == 123.45


def test_rows_to_list_empty():
    assert _rows_to_list([]) == []
