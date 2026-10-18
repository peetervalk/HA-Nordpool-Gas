"""Unit tests for the pure (layer-1) functions in sensor.py."""
from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime, time, timedelta, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import aiohttp
import pytest
from pynordpool import NordPoolEmptyResponseError, NordPoolError

import homeassistant.util.dt as dt_util

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
    SENSORS,
    SpotPriceSensor,
    _apply_electricity_fees,
    _build_gas_series,
    _build_hourly_averages,
    _build_hourly_rows,
    _current_hour_average,
    _current_slot_price,
    _delivery_dates,
    _describe_error,
    _ElectricityPrices,
    _gas_prices_by_day,
    _GasPrices,
    _make_transfer_fn,
    _rows_to_list,
)
from custom_components.ha_nordpool_gas.eexgas import EexGasConnectionError, GasDayPrice, Hub

TALLINN = ZoneInfo("Europe/Tallinn")


# ---------------------------------------------------------------------------
# Data helpers
# ---------------------------------------------------------------------------


def _entry(local_dt: datetime, price: float) -> tuple[datetime, float]:
    """A Nord Pool (start, EUR/MWh) entry; pynordpool gives starts as UTC datetimes."""
    return (local_dt.astimezone(timezone.utc), price)


def _gas_rows(*rows: tuple[date, float]) -> list[GasDayPrice]:
    """EEX gas rows as the eexgas client returns them: gas day, price."""
    return [GasDayPrice(gas_day, price, 1000.0, "Final NGP", None) for gas_day, price in rows]


def _epoch(dt_aware: datetime) -> int:
    return int(dt_aware.timestamp())


# ---------------------------------------------------------------------------
# _delivery_dates
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "today",
    [
        date(2026, 10, 19),  # EEST
        date(2026, 10, 25),  # clocks go back: 25-hour local day
        date(2026, 3, 29),  # clocks go forward: 23-hour local day
        date(2026, 1, 15),  # EET
    ],
)
def test_delivery_dates_tallinn_spans_yesterday_to_tomorrow(today):
    """Local midnight in Tallinn is 23:00 CET the day before, so yesterday's CET day is needed."""
    tomorrow = today + timedelta(days=1)
    assert _delivery_dates(today, tomorrow) == [today - timedelta(days=1), today, tomorrow]


def test_delivery_dates_for_tallinn_tomorrow_alone_are_today_and_tomorrow():
    """Local tomorrow's first hour is the last hour of today's CET day."""
    today = date(2026, 10, 19)
    tomorrow = today + timedelta(days=1)
    assert _delivery_dates(tomorrow, tomorrow) == [today, tomorrow]


def test_delivery_dates_west_of_cet_spans_today_to_day_after_tomorrow():
    """In London local tomorrow ends at 01:00 CET the day after, which needs that CET day too."""
    dt_util.set_default_time_zone(ZoneInfo("Europe/London"))
    today = date(2026, 10, 19)
    tomorrow = today + timedelta(days=1)
    assert _delivery_dates(today, tomorrow) == [today, tomorrow, today + timedelta(days=2)]


def test_delivery_dates_in_cet_are_today_and_tomorrow():
    dt_util.set_default_time_zone(ZoneInfo("Europe/Oslo"))
    today = date(2026, 10, 19)
    tomorrow = today + timedelta(days=1)
    assert _delivery_dates(today, tomorrow) == [today, tomorrow]


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
# _gas_prices_by_day
# ---------------------------------------------------------------------------

GAS_YESTERDAY = date(2024, 6, 14)
GAS_TODAY = date(2024, 6, 15)
GAS_TOMORROW = date(2024, 6, 16)
GAS_DAY_AFTER = date(2024, 6, 17)


def test_gas_prices_by_day_today_and_tomorrow():
    rows = _gas_rows((GAS_TODAY, 45.5), (GAS_TOMORROW, 46.0))
    assert _gas_prices_by_day(rows) == {GAS_TODAY: 45.5, GAS_TOMORROW: 46.0}


def test_gas_prices_by_day_zero_price_skipped():
    # EEX lists the next gas day at 0 until it has been traded
    rows = _gas_rows((GAS_TODAY, 0.0), (GAS_TODAY, 45.5), (GAS_TOMORROW, 0.0))
    assert _gas_prices_by_day(rows) == {GAS_TODAY: 45.5}


def test_gas_prices_by_day_first_nonzero_wins():
    rows = _gas_rows((GAS_TODAY, 45.5), (GAS_TODAY, 99.0))
    assert _gas_prices_by_day(rows) == {GAS_TODAY: 45.5}


def test_gas_prices_by_day_no_rows():
    assert _gas_prices_by_day([]) == {}


# ---------------------------------------------------------------------------
# _GasPrices
# ---------------------------------------------------------------------------


class _FakeGasClient:
    """Answers async_get_prices with `result`: a list of rows, or an exception to raise."""

    def __init__(self, result) -> None:
        self.result = result

    async def async_get_prices(self, hub):
        if isinstance(self.result, BaseException):
            raise self.result
        return self.result


def _gas_after_update(client: _FakeGasClient, today: date = GAS_TODAY, now_ts: int = 1000) -> _GasPrices:
    gas = _GasPrices(client, Hub.LVA_EST)  # type: ignore[arg-type]
    asyncio.run(gas.async_update(today, now_ts))
    return gas


def test_gas_prices_remembers_today_and_tomorrow_with_fetch_time():
    client = _FakeGasClient(
        _gas_rows((GAS_YESTERDAY, 40.0), (GAS_TODAY, 45.5), (GAS_TOMORROW, 46.0), (GAS_DAY_AFTER, 0.0))
    )
    gas = _gas_after_update(client)

    assert gas.get(GAS_TODAY) == (45.5, 1000)
    assert gas.get(GAS_TOMORROW) == (46.0, 1000)
    assert gas.get(GAS_DAY_AFTER) is None
    assert gas.get(GAS_YESTERDAY) is None


def test_gas_prices_failed_fetch_keeps_prices_and_their_fetch_time():
    client = _FakeGasClient(_gas_rows((GAS_TODAY, 45.5), (GAS_TOMORROW, 46.0)))
    gas = _gas_after_update(client)

    client.result = EexGasConnectionError("Connection reset by peer")
    asyncio.run(gas.async_update(GAS_TODAY, 1900))

    assert gas.get(GAS_TODAY) == (45.5, 1000)
    assert gas.get(GAS_TOMORROW) == (46.0, 1000)


def test_gas_prices_successful_fetch_refreshes_price_and_fetch_time():
    client = _FakeGasClient(_gas_rows((GAS_TODAY, 45.5)))
    gas = _gas_after_update(client)

    client.result = _gas_rows((GAS_TODAY, 45.7))
    asyncio.run(gas.async_update(GAS_TODAY, 1900))

    assert gas.get(GAS_TODAY) == (45.7, 1900)


def test_gas_prices_tomorrow_becomes_today_at_midnight_even_if_the_fetch_fails():
    client = _FakeGasClient(_gas_rows((GAS_TODAY, 45.5), (GAS_TOMORROW, 46.0)))
    gas = _gas_after_update(client)

    client.result = EexGasConnectionError("Connection reset by peer")
    asyncio.run(gas.async_update(GAS_TOMORROW, 2000))

    assert gas.get(GAS_TOMORROW) == (46.0, 1000)
    assert gas.get(GAS_TODAY) is None  # yesterday by now
    assert gas.get(GAS_DAY_AFTER) is None


def test_gas_prices_tomorrow_is_not_carried_to_the_next_day():
    """After midnight the new tomorrow is a zero placeholder: it must stay unknown."""
    client = _FakeGasClient(_gas_rows((GAS_TODAY, 45.5), (GAS_TOMORROW, 46.0)))
    gas = _gas_after_update(client)

    client.result = _gas_rows((GAS_TODAY, 45.6), (GAS_TOMORROW, 46.2), (GAS_DAY_AFTER, 0.0))
    asyncio.run(gas.async_update(GAS_TOMORROW, 2000))

    assert gas.get(GAS_TOMORROW) == (46.2, 2000)
    assert gas.get(GAS_DAY_AFTER) is None


def test_gas_prices_day_missing_from_a_later_file_keeps_its_remembered_price():
    client = _FakeGasClient(_gas_rows((GAS_TODAY, 45.5), (GAS_TOMORROW, 46.0)))
    gas = _gas_after_update(client)

    client.result = _gas_rows((GAS_TODAY, 45.6), (GAS_TOMORROW, 0.0))
    asyncio.run(gas.async_update(GAS_TODAY, 1900))

    assert gas.get(GAS_TODAY) == (45.6, 1900)
    assert gas.get(GAS_TOMORROW) == (46.0, 1000)


def test_gas_prices_logs_a_failure_once(caplog):
    caplog.set_level(logging.DEBUG, logger=SENSOR_LOGGER)
    gas = _gas_after_update(_FakeGasClient(EexGasConnectionError("Connection reset by peer")))
    asyncio.run(gas.async_update(GAS_TODAY, 1900))

    assert [(r.levelno, r.getMessage()) for r in caplog.records if r.name == SENSOR_LOGGER] == [
        (logging.WARNING, "Gas price fetch failed, keeping the last known prices: Connection reset by peer"),
        (logging.DEBUG, "Gas price fetch still failing: Connection reset by peer"),
    ]


# ---------------------------------------------------------------------------
# _apply_electricity_fees
# ---------------------------------------------------------------------------


def test_apply_electricity_fees_vat_applied():
    today = date(2024, 6, 15)
    entries = [_entry(datetime(2024, 6, 15, 12, 0, tzinfo=TALLINN), 50.0)]
    no_transfer = _make_transfer_fn({CONF_TRANSFER_MODE: TRANSFER_MODE_NONE})

    today_rows, _ = _apply_electricity_fees(entries, 22.0, no_transfer, today, today + timedelta(days=1))

    assert len(today_rows) == 1
    # 50.0 * 1.22 = 61.0
    assert today_rows[0][1] == pytest.approx(61.0)


def test_apply_electricity_fees_transfer_fee_applied():
    today = date(2024, 6, 15)
    entries = [_entry(datetime(2024, 6, 15, 14, 0, tzinfo=TALLINN), 50.0)]
    fixed_transfer = _make_transfer_fn(
        {CONF_TRANSFER_MODE: TRANSFER_MODE_FIXED, CONF_TRANSFER_FIXED: 5.0}
    )

    today_rows, _ = _apply_electricity_fees(entries, 22.0, fixed_transfer, today, today + timedelta(days=1))

    # (50 + 5) * 1.22 = 67.1
    assert today_rows[0][1] == pytest.approx(67.1)


def test_apply_electricity_fees_today_and_tomorrow_split():
    today = date(2024, 6, 15)
    tomorrow = date(2024, 6, 16)
    entries = [
        _entry(datetime(2024, 6, 15, 12, 0, tzinfo=TALLINN), 50.0),
        _entry(datetime(2024, 6, 16, 12, 0, tzinfo=TALLINN), 60.0),
    ]
    no_transfer = _make_transfer_fn({CONF_TRANSFER_MODE: TRANSFER_MODE_NONE})

    today_rows, tomorrow_rows = _apply_electricity_fees(entries, 0.0, no_transfer, today, tomorrow)

    assert len(today_rows) == 1
    assert len(tomorrow_rows) == 1


def test_apply_electricity_fees_midnight_row_classified_as_local_date():
    """00:15 local time is 21:15 UTC the previous calendar day.
    The row must be classified by local date, not UTC date."""
    today = date(2024, 6, 15)
    # 00:15 EEST on 2024-06-15 = 21:15 UTC on 2024-06-14
    entries = [_entry(datetime(2024, 6, 15, 0, 15, tzinfo=TALLINN), 50.0)]
    no_transfer = _make_transfer_fn({CONF_TRANSFER_MODE: TRANSFER_MODE_NONE})

    today_rows, tomorrow_rows = _apply_electricity_fees(
        entries, 0.0, no_transfer, today, today + timedelta(days=1)
    )

    assert len(today_rows) == 1, "00:15 local should be today, not skipped as UTC-previous-day"
    assert len(tomorrow_rows) == 0


def test_apply_electricity_fees_rows_are_local_and_timezone_aware():
    """Returned datetimes must be timezone-aware (not naive) for correct timestamp serialisation."""
    today = date(2024, 6, 15)
    entries = [_entry(datetime(2024, 6, 15, 12, 0, tzinfo=TALLINN), 50.0)]
    no_transfer = _make_transfer_fn({CONF_TRANSFER_MODE: TRANSFER_MODE_NONE})

    today_rows, _ = _apply_electricity_fees(entries, 0.0, no_transfer, today, today + timedelta(days=1))

    dt, _ = today_rows[0]
    assert dt.tzinfo is not None
    assert dt.hour == 12


def test_apply_electricity_fees_transfer_fn_sees_local_time():
    """Day/night tariffs depend on the local hour, not the UTC hour Nord Pool reports."""
    today = date(2024, 6, 17)  # Monday
    entries = [_entry(datetime(2024, 6, 17, 7, 0, tzinfo=TALLINN), 50.0)]  # 04:00 UTC
    day_night = _make_transfer_fn(
        {
            CONF_TRANSFER_MODE: TRANSFER_MODE_DAY_NIGHT,
            CONF_TRANSFER_DAY: 10.0,
            CONF_TRANSFER_NIGHT: 1.0,
            CONF_TRANSFER_DAY_START: 7,
            CONF_TRANSFER_DAY_END: 22,
        }
    )

    today_rows, _ = _apply_electricity_fees(entries, 0.0, day_night, today, today + timedelta(days=1))

    assert today_rows[0][1] == pytest.approx(60.0)


def test_apply_electricity_fees_out_of_range_rows_discarded():
    today = date(2024, 6, 15)
    tomorrow = date(2024, 6, 16)
    entries = [
        _entry(datetime(2024, 6, 14, 12, 0, tzinfo=TALLINN), 40.0),
        _entry(datetime(2024, 6, 17, 12, 0, tzinfo=TALLINN), 80.0),
    ]
    no_transfer = _make_transfer_fn({CONF_TRANSFER_MODE: TRANSFER_MODE_NONE})

    today_rows, tomorrow_rows = _apply_electricity_fees(entries, 0.0, no_transfer, today, tomorrow)

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
# _build_hourly_rows
# ---------------------------------------------------------------------------


def _day_rows(day: date) -> list[tuple[datetime, float]]:
    """Every 15-min slot of a local day, priced by the index of its real (elapsed) hour."""
    start = _epoch(datetime.combine(day, time(), tzinfo=TALLINN))
    end = _epoch(datetime.combine(day + timedelta(days=1), time(), tzinfo=TALLINN))
    return [
        (datetime.fromtimestamp(ts, tz=TALLINN), float((ts - start) // 3600))
        for ts in range(start, end, 900)
    ]


def test_build_hourly_rows_one_row_per_hour_at_hour_start():
    rows = [
        (datetime(2024, 6, 15, 10, 0, tzinfo=TALLINN), 40.0),
        (datetime(2024, 6, 15, 10, 15, tzinfo=TALLINN), 50.0),
        (datetime(2024, 6, 15, 10, 30, tzinfo=TALLINN), 60.0),
        (datetime(2024, 6, 15, 10, 45, tzinfo=TALLINN), 50.0),
        (datetime(2024, 6, 15, 11, 0, tzinfo=TALLINN), 80.0),
        (datetime(2024, 6, 15, 11, 15, tzinfo=TALLINN), 60.0),
    ]

    result = _build_hourly_rows(rows)

    assert result == [
        [_epoch(datetime(2024, 6, 15, 10, 0, tzinfo=TALLINN)), 50.0],
        [_epoch(datetime(2024, 6, 15, 11, 0, tzinfo=TALLINN)), 70.0],
    ]
    assert all(type(ts) is int for ts, _ in result)


def test_build_hourly_rows_matches_hourly_averages_on_a_normal_day():
    rows = _day_rows(date(2024, 6, 15))

    hourly_rows = _build_hourly_rows(rows)

    assert len(hourly_rows) == 24
    by_hour = {datetime.fromtimestamp(ts, tz=TALLINN).hour: price for ts, price in hourly_rows}
    assert by_hour == _build_hourly_averages(rows)


def test_build_hourly_rows_empty():
    assert _build_hourly_rows([]) == []


@pytest.mark.parametrize(
    ("day", "hours"),
    [
        (date(2026, 3, 29), 23),  # springs forward (EET -> EEST), 03:00 is skipped
        (date(2026, 10, 25), 25),  # falls back (EEST -> EET), 03:00 occurs twice
    ],
)
def test_build_hourly_rows_dst_day_has_one_row_per_real_hour(day, hours):
    hourly_rows = _build_hourly_rows(_day_rows(day))

    timestamps = [ts for ts, _ in hourly_rows]
    assert len(hourly_rows) == hours
    assert [b - a for a, b in zip(timestamps, timestamps[1:])] == [3600] * (hours - 1)
    # Each row carries its own hour's price, so the repeated hour is not merged.
    assert [price for _, price in hourly_rows] == [float(n) for n in range(hours)]


# ---------------------------------------------------------------------------
# _current_slot_price / _current_hour_average
# ---------------------------------------------------------------------------


def _fall_back_rows() -> list[tuple[datetime, float]]:
    """Both passes of 03:00-04:00 on 2026-10-25: 300.0 in EEST, then 400.0 in EET."""
    return [
        (datetime(2026, 10, 25, utc_hour, minute, tzinfo=timezone.utc).astimezone(TALLINN), price)
        for utc_hour, price in ((0, 300.0), (1, 400.0))
        for minute in (0, 15, 30, 45)
    ]


def test_current_slot_price_matches_slot_containing_now():
    rows = [
        (datetime(2024, 6, 15, 10, 0, tzinfo=TALLINN), 40.0),
        (datetime(2024, 6, 15, 10, 15, tzinfo=TALLINN), 50.0),
        (datetime(2024, 6, 15, 10, 30, tzinfo=TALLINN), 60.0),
    ]

    assert _current_slot_price(rows, datetime(2024, 6, 15, 10, 15, tzinfo=TALLINN)) == 50.0
    assert _current_slot_price(rows, datetime(2024, 6, 15, 10, 29, 59, tzinfo=TALLINN)) == 50.0


def test_current_slot_price_ignores_same_clock_time_on_another_day():
    """Yesterday's 10:15 row must not be served as today's 10:15 price."""
    rows = [(datetime(2024, 6, 14, 10, 15, tzinfo=TALLINN), 50.0)]

    assert _current_slot_price(rows, datetime(2024, 6, 15, 10, 20, tzinfo=TALLINN)) is None


def test_current_slot_price_dst_fall_back_keeps_passes_apart():
    """03:15 occurs twice on the fall-back day; each pass has its own price."""
    rows = _fall_back_rows()
    first_pass = datetime(2026, 10, 25, 0, 20, tzinfo=timezone.utc).astimezone(TALLINN)
    second_pass = datetime(2026, 10, 25, 1, 20, tzinfo=timezone.utc).astimezone(TALLINN)
    assert (first_pass.hour, first_pass.minute) == (second_pass.hour, second_pass.minute) == (3, 20)

    assert _current_slot_price(rows, first_pass) == 300.0
    assert _current_slot_price(rows, second_pass) == 400.0


def test_current_hour_average_uses_only_the_current_hour():
    rows = [
        (datetime(2024, 6, 15, 9, 45, tzinfo=TALLINN), 10.0),
        (datetime(2024, 6, 15, 10, 0, tzinfo=TALLINN), 40.0),
        (datetime(2024, 6, 15, 10, 15, tzinfo=TALLINN), 50.0),
        (datetime(2024, 6, 15, 10, 30, tzinfo=TALLINN), 60.0),
        (datetime(2024, 6, 15, 10, 45, tzinfo=TALLINN), 50.0),
        (datetime(2024, 6, 15, 11, 0, tzinfo=TALLINN), 90.0),
    ]
    now = datetime(2024, 6, 15, 10, 40, tzinfo=TALLINN)

    assert _current_hour_average(rows, now) == pytest.approx(50.0)
    assert _current_hour_average(rows, now) == _build_hourly_averages(rows)[10]


def test_current_hour_average_ignores_same_hour_on_another_day():
    rows = [(datetime(2024, 6, 14, 10, 0, tzinfo=TALLINN), 40.0)]

    assert _current_hour_average(rows, datetime(2024, 6, 15, 10, 0, tzinfo=TALLINN)) is None


def test_current_hour_average_dst_fall_back_keeps_passes_apart():
    rows = _fall_back_rows()
    first_pass = datetime(2026, 10, 25, 0, 20, tzinfo=timezone.utc).astimezone(TALLINN)
    second_pass = datetime(2026, 10, 25, 1, 20, tzinfo=timezone.utc).astimezone(TALLINN)

    assert _current_hour_average(rows, first_pass) == pytest.approx(300.0)
    assert _current_hour_average(rows, second_pass) == pytest.approx(400.0)


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


# ---------------------------------------------------------------------------
# _ElectricityPrices
# ---------------------------------------------------------------------------

AREA = "EE"
CET = ZoneInfo("Europe/Oslo")
D17, D18, D19, D20 = (date(2026, 10, day) for day in (17, 18, 19, 20))
SENSOR_LOGGER = "custom_components.ha_nordpool_gas.sensor"


def _period(day: date, prices: list[float], final: bool = True, area: str = AREA) -> SimpleNamespace:
    """A pynordpool DeliveryPeriodData stand-in: 15-min entries from CET midnight of `day`."""
    start = datetime.combine(day, time(), tzinfo=CET).astimezone(timezone.utc)
    entries = [
        SimpleNamespace(start=start + timedelta(minutes=15 * i), entry={area: price})
        for i, price in enumerate(prices)
    ]
    return SimpleNamespace(entries=entries, prices_final=final)


class _FakeNordPoolClient:
    """Answers each delivery day from `results`, a {date: period or exception} map.

    Days missing from the map answer as unpublished, like Nord Pool's HTTP 204.
    """

    def __init__(self, results: dict) -> None:
        self.results = results
        self.requested: list[date] = []

    async def async_get_delivery_period(self, target_date, currency, areas):
        day = target_date.date()
        self.requested.append(day)
        result = self.results.get(day, NordPoolEmptyResponseError("Empty response"))
        if isinstance(result, BaseException):
            raise result
        return result


def _get_prices(electricity: _ElectricityPrices, days: list[date]) -> list[float]:
    return [price for _, price in asyncio.run(electricity.async_entries(days))]


def test_electricity_prices_fetches_every_day_and_returns_entries_in_day_order():
    client = _FakeNordPoolClient(
        {D17: _period(D17, [1.0, 2.0]), D18: _period(D18, [3.0]), D19: _period(D19, [4.0])}
    )
    electricity = _ElectricityPrices(client, AREA)

    assert _get_prices(electricity, [D17, D18, D19]) == [1.0, 2.0, 3.0, 4.0]
    assert sorted(client.requested) == [D17, D18, D19]


def test_electricity_prices_entries_start_at_utc_instants():
    client = _FakeNordPoolClient({D19: _period(D19, [4.0])})
    electricity = _ElectricityPrices(client, AREA)

    ((start, _),) = asyncio.run(electricity.async_entries([D19]))

    assert start == datetime(2026, 10, 18, 22, 0, tzinfo=timezone.utc)  # 00:00 CEST


def test_electricity_prices_final_days_are_not_fetched_again():
    client = _FakeNordPoolClient({D18: _period(D18, [3.0]), D19: _period(D19, [4.0])})
    electricity = _ElectricityPrices(client, AREA)
    _get_prices(electricity, [D18, D19])
    client.requested.clear()

    assert _get_prices(electricity, [D18, D19]) == [3.0, 4.0]
    assert client.requested == []


def test_electricity_prices_preliminary_day_is_used_and_fetched_again():
    client = _FakeNordPoolClient({D19: _period(D19, [4.0], final=False)})
    electricity = _ElectricityPrices(client, AREA)
    assert _get_prices(electricity, [D19]) == [4.0]

    client.results[D19] = _period(D19, [4.5])
    assert _get_prices(electricity, [D19]) == [4.5]

    client.requested.clear()
    _get_prices(electricity, [D19])
    assert client.requested == []


def test_electricity_prices_unpublished_day_is_asked_again_without_logging(caplog):
    caplog.set_level(logging.DEBUG, logger=SENSOR_LOGGER)
    client = _FakeNordPoolClient({D19: _period(D19, [4.0])})
    electricity = _ElectricityPrices(client, AREA)
    _get_prices(electricity, [D19, D20])
    client.requested.clear()

    assert _get_prices(electricity, [D19, D20]) == [4.0]
    assert client.requested == [D20]
    assert [r for r in caplog.records if r.name == SENSOR_LOGGER] == []


@pytest.mark.parametrize(
    "error",
    [NordPoolError(), aiohttp.ClientError("Connection reset by peer"), TimeoutError()],
    ids=["nordpool", "aiohttp", "timeout"],
)
def test_electricity_prices_failed_fetch_keeps_days_already_held(error):
    client = _FakeNordPoolClient({D18: _period(D18, [3.0]), D19: _period(D19, [4.0], final=False)})
    electricity = _ElectricityPrices(client, AREA)
    _get_prices(electricity, [D18, D19])

    client.results = {D19: error, D20: error}

    assert _get_prices(electricity, [D18, D19, D20]) == [3.0, 4.0]


def test_electricity_prices_after_midnight_held_days_carry_over_and_old_days_drop():
    """A failed fetch at midnight still leaves the previous 'tomorrow' to serve as today."""
    client = _FakeNordPoolClient({day: _period(day, [float(day.day)]) for day in (D17, D18, D19)})
    electricity = _ElectricityPrices(client, AREA)
    _get_prices(electricity, [D17, D18, D19])
    client.results = {D20: NordPoolError()}
    client.requested.clear()

    assert _get_prices(electricity, [D18, D19, D20]) == [18.0, 19.0]
    assert client.requested == [D20]

    # D17 was dropped, so asking for it again fetches it
    client.requested.clear()
    _get_prices(electricity, [D17])
    assert client.requested == [D17]


def test_electricity_prices_holds_fetched_days_final_or_preliminary():
    client = _FakeNordPoolClient({D19: _period(D19, [4.0]), D20: _period(D20, [5.0], final=False)})
    electricity = _ElectricityPrices(client, AREA)
    _get_prices(electricity, [D19, D20])

    assert electricity.holds([D19, D20])


def test_electricity_prices_does_not_hold_unpublished_or_empty_days():
    """tomorrow_valid stays false until Nord Pool has published tomorrow's CET day."""
    client = _FakeNordPoolClient({D19: _period(D19, [4.0]), D20: _period(D20, [5.0], area="FI")})
    electricity = _ElectricityPrices(client, AREA)
    _get_prices(electricity, [D18, D19, D20])

    assert electricity.holds([D19])
    assert not electricity.holds([D19, D20])  # D20 has no prices for this area
    assert not electricity.holds([D18])  # D18 is unpublished


def _cet_day(day: date, price: float) -> SimpleNamespace:
    """A whole published CET delivery day at 15-min resolution: 92, 96 or 100 entries."""
    start = datetime.combine(day, time(), tzinfo=CET).astimezone(timezone.utc)
    end = datetime.combine(day + timedelta(days=1), time(), tzinfo=CET).astimezone(timezone.utc)
    count = (end - start) // timedelta(minutes=15)
    entries = [SimpleNamespace(start=start + timedelta(minutes=15 * i), entry={AREA: price}) for i in range(count)]
    return SimpleNamespace(entries=entries, prices_final=True)


@pytest.mark.parametrize(
    ("today", "today_slots", "tomorrow_slots"),
    [
        (date(2026, 10, 24), 96, 100),  # tomorrow is the 25-hour day
        (date(2026, 10, 25), 100, 96),  # clocks go back at 04:00 EEST
        (date(2026, 3, 28), 96, 92),  # tomorrow is the 23-hour day
        (date(2026, 3, 29), 92, 96),  # clocks go forward at 03:00 EET
    ],
)
def test_clock_change_days_get_every_local_slot_and_tomorrow_valid(today, today_slots, tomorrow_slots):
    tomorrow = today + timedelta(days=1)
    days = _delivery_dates(today, tomorrow)
    client = _FakeNordPoolClient({day: _cet_day(day, float(day.day)) for day in days})
    electricity = _ElectricityPrices(client, AREA)
    no_transfer = _make_transfer_fn({CONF_TRANSFER_MODE: TRANSFER_MODE_NONE})

    entries = asyncio.run(electricity.async_entries(days))
    today_rows, tomorrow_rows = _apply_electricity_fees(entries, 0.0, no_transfer, today, tomorrow)

    assert len(today_rows) == today_slots
    assert len(tomorrow_rows) == tomorrow_slots
    assert today_rows[0][0] == dt_util.start_of_local_day(today)
    assert tomorrow_rows[-1][0] + timedelta(minutes=15) == dt_util.start_of_local_day(tomorrow + timedelta(days=1))
    assert electricity.holds(_delivery_dates(tomorrow, tomorrow))

    # Before Nord Pool publishes tomorrow's CET day, tomorrow is not valid
    del client.results[days[-1]]
    unpublished = _ElectricityPrices(client, AREA)
    asyncio.run(unpublished.async_entries(days))
    assert not unpublished.holds(_delivery_dates(tomorrow, tomorrow))


def test_electricity_prices_ignores_entries_for_other_areas():
    client = _FakeNordPoolClient({D19: _period(D19, [4.0], area="FI")})
    electricity = _ElectricityPrices(client, AREA)

    assert _get_prices(electricity, [D19]) == []


def test_electricity_prices_logs_a_failure_once_and_its_recovery(caplog):
    caplog.set_level(logging.DEBUG, logger=SENSOR_LOGGER)
    client = _FakeNordPoolClient({D19: NordPoolError("API error: 503")})
    electricity = _ElectricityPrices(client, AREA)

    _get_prices(electricity, [D19])
    _get_prices(electricity, [D19])
    client.results = {D19: _period(D19, [4.0])}
    _get_prices(electricity, [D19])

    assert [(r.levelno, r.getMessage()) for r in caplog.records if r.name == SENSOR_LOGGER] == [
        (logging.WARNING, "Electricity price fetch failed, keeping the last known prices: API error: 503"),
        (logging.DEBUG, "Electricity price fetch still failing: API error: 503"),
        (logging.INFO, "Electricity price fetch recovered"),
    ]


# ---------------------------------------------------------------------------
# _describe_error
# ---------------------------------------------------------------------------


def test_describe_error_uses_the_message():
    assert _describe_error(ValueError("boom")) == "boom"


def test_describe_error_falls_back_to_the_cause():
    """pynordpool raises a bare NordPoolError from the last error once its retries run out."""
    err = NordPoolError()
    err.__cause__ = aiohttp.ClientError("Connection reset by peer")
    assert _describe_error(err) == "Connection reset by peer"


def test_describe_error_falls_back_to_the_type_name():
    assert _describe_error(TimeoutError()) == "TimeoutError"


# ---------------------------------------------------------------------------
# _build_gas_series
# ---------------------------------------------------------------------------


def test_build_gas_series_uses_gas_tomorrow_when_known():
    series = _build_gas_series(45.5, 46.0, date(2024, 6, 15))

    assert series == [
        [_epoch(datetime(2024, 6, 15, tzinfo=TALLINN)), 45.5],
        [_epoch(datetime(2024, 6, 16, tzinfo=TALLINN)), 46.0],
        # End point repeats tomorrow's price to close the step at tomorrow 24:00
        [_epoch(datetime(2024, 6, 17, tzinfo=TALLINN)), 46.0],
    ]
    assert all(type(ts) is int for ts, _ in series)


def test_build_gas_series_falls_back_to_gas_now_without_tomorrow():
    """Tomorrow is still covered so the chart's X axis reaches tomorrow 24:00."""
    series = _build_gas_series(45.5, None, date(2024, 6, 15))

    assert series == [
        [_epoch(datetime(2024, 6, 15, tzinfo=TALLINN)), 45.5],
        [_epoch(datetime(2024, 6, 16, tzinfo=TALLINN)), 45.5],
        [_epoch(datetime(2024, 6, 17, tzinfo=TALLINN)), 45.5],
    ]


@pytest.mark.parametrize("gas_tomorrow", [None, 46.0])
def test_build_gas_series_empty_without_gas_now(gas_tomorrow):
    assert _build_gas_series(None, gas_tomorrow, date(2024, 6, 15)) == []


@pytest.mark.parametrize(
    ("today", "day_hours"),
    [
        (date(2026, 3, 29), (23, 24)),  # today springs forward (EET -> EEST)
        (date(2026, 3, 28), (24, 23)),  # tomorrow springs forward
        (date(2026, 10, 25), (25, 24)),  # today falls back (EEST -> EET)
        (date(2026, 10, 24), (24, 25)),  # tomorrow falls back
    ],
)
def test_build_gas_series_dst_rows_on_local_midnights(today, day_hours):
    """23h/25h days must not push rows off local midnight (no fixed 24h steps)."""
    series = _build_gas_series(45.5, 46.0, today)

    local = [datetime.fromtimestamp(ts, tz=TALLINN) for ts, _ in series]
    assert [dt.date() for dt in local] == [today + timedelta(days=n) for n in range(3)]
    assert all(dt.time() == time(0, 0) for dt in local)
    timestamps = [ts for ts, _ in series]
    assert [b - a for a, b in zip(timestamps, timestamps[1:])] == [h * 3600 for h in day_hours]


# ---------------------------------------------------------------------------
# SpotPriceSensor.extra_state_attributes
# ---------------------------------------------------------------------------


def test_gas_sensor_exposes_gas_series_without_recording_it():
    gas_description = next(d for d in SENSORS if d.key == "gas_now")
    sensor = SpotPriceSensor(None, "entry", "Spot Price", gas_description)
    series = _build_gas_series(45.5, 46.0, date(2024, 6, 15))
    sensor.coordinator = SimpleNamespace(
        data={"updated_at": 1718450000, "gas_tomorrow": 46.0, "gas_series": series}
    )

    assert sensor.extra_state_attributes == {
        "updated_at": 1718450000,
        "gas_tomorrow": 46.0,
        "gas_series": series,
    }
    assert "gas_series" in SpotPriceSensor._unrecorded_attributes


@pytest.mark.parametrize("key", ["electricity_15min", "electricity_hourly"])
def test_electricity_sensors_expose_hourly_rows_without_recording_them(key):
    description = next(d for d in SENSORS if d.key == key)
    sensor = SpotPriceSensor(None, "entry", "Spot Price", description)
    hourly_rows = _build_hourly_rows(_day_rows(date(2024, 6, 15)))
    sensor.coordinator = SimpleNamespace(
        data={"hourly_rows_today": hourly_rows, "hourly_rows_tomorrow": []}
    )

    attrs = sensor.extra_state_attributes

    assert attrs["hourly_rows_today"] == hourly_rows
    assert attrs["hourly_rows_tomorrow"] == []
    assert {"hourly_rows_today", "hourly_rows_tomorrow"} <= SpotPriceSensor._unrecorded_attributes
