"""Unit tests for the pure (layer-1) functions in sensor.py."""
from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from types import SimpleNamespace
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
    SENSORS,
    SpotPriceSensor,
    _build_elering_url,
    _build_gas_series,
    _build_hourly_averages,
    _build_hourly_rows,
    _current_hour_average,
    _current_slot_price,
    _make_transfer_fn,
    _parse_electricity_csv,
    _retained_electricity_rows,
    _rows_to_list,
    _select_gas_prices,
)
from custom_components.ha_nordpool_gas.eexgas import GasDayPrice

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


def _gas_rows(*rows: tuple[date, float]) -> list[GasDayPrice]:
    """EEX gas rows as the eexgas client returns them: gas day, price."""
    return [GasDayPrice(gas_day, price, 1000.0, "Final NGP", None) for gas_day, price in rows]


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
# _select_gas_prices
# ---------------------------------------------------------------------------

GAS_TODAY = date(2024, 6, 15)
GAS_TOMORROW = date(2024, 6, 16)


def test_select_gas_prices_today_and_tomorrow():
    rows = _gas_rows((GAS_TODAY, 45.5), (GAS_TOMORROW, 46.0))
    today, tomorrow = _select_gas_prices(rows, GAS_TODAY, GAS_TOMORROW)
    assert today == 45.5
    assert tomorrow == 46.0


def test_select_gas_prices_zero_price_skipped():
    # First row is zero → skip; second row is the real price
    rows = _gas_rows((GAS_TODAY, 0.0), (GAS_TODAY, 45.5))
    today, _ = _select_gas_prices(rows, GAS_TODAY, GAS_TOMORROW)
    assert today == 45.5


def test_select_gas_prices_tomorrow_absent():
    rows = _gas_rows((GAS_TODAY, 45.5))
    today, tomorrow = _select_gas_prices(rows, GAS_TODAY, GAS_TOMORROW)
    assert today == 45.5
    assert tomorrow is None


def test_select_gas_prices_first_nonzero_wins():
    rows = _gas_rows((GAS_TODAY, 45.5), (GAS_TODAY, 99.0))
    today, _ = _select_gas_prices(rows, GAS_TODAY, GAS_TOMORROW)
    assert today == 45.5


def test_select_gas_prices_irrelevant_dates_ignored():
    rows = _gas_rows((date(2024, 6, 13), 40.0), (GAS_TODAY, 45.5))
    today, tomorrow = _select_gas_prices(rows, GAS_TODAY, GAS_TOMORROW)
    assert today == 45.5
    assert tomorrow is None


def test_select_gas_prices_no_rows():
    today, tomorrow = _select_gas_prices([], GAS_TODAY, GAS_TOMORROW)
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
# _retained_electricity_rows
# ---------------------------------------------------------------------------


def _prev_data(
    today_rows: list[tuple[datetime, float]], tomorrow_rows: list[tuple[datetime, float]]
) -> dict:
    """Coordinator data as a previous update would have stored it."""
    return {
        "electricity_rows_today": _rows_to_list(today_rows),
        "electricity_rows_tomorrow": _rows_to_list(tomorrow_rows),
    }


def test_retained_electricity_rows_same_day_keeps_buckets():
    row_15 = (datetime(2024, 6, 15, 12, 0, tzinfo=TALLINN), 50.0)
    row_16 = (datetime(2024, 6, 16, 12, 0, tzinfo=TALLINN), 60.0)
    prev = _prev_data([row_15], [row_16])

    today_rows, tomorrow_rows = _retained_electricity_rows(prev, date(2024, 6, 15), date(2024, 6, 16))

    assert today_rows == [row_15]
    assert tomorrow_rows == [row_16]
    assert today_rows[0][0].tzinfo is not None


def test_retained_electricity_rows_after_midnight_promotes_tomorrow():
    """Once the day rolls over, the stored 'tomorrow' is today and the stored 'today' is gone."""
    row_15 = (datetime(2024, 6, 15, 0, 0, tzinfo=TALLINN), 50.0)
    row_16 = (datetime(2024, 6, 16, 0, 0, tzinfo=TALLINN), 60.0)
    prev = _prev_data([row_15], [row_16])

    today_rows, tomorrow_rows = _retained_electricity_rows(prev, date(2024, 6, 16), date(2024, 6, 17))

    assert today_rows == [row_16]
    assert tomorrow_rows == []


def test_retained_electricity_rows_nothing_left_after_two_midnights():
    row_15 = (datetime(2024, 6, 15, 12, 0, tzinfo=TALLINN), 50.0)
    row_16 = (datetime(2024, 6, 16, 12, 0, tzinfo=TALLINN), 60.0)
    prev = _prev_data([row_15], [row_16])

    assert _retained_electricity_rows(prev, date(2024, 6, 17), date(2024, 6, 18)) == ([], [])


@pytest.mark.parametrize(
    "prev",
    [{}, {"electricity_rows_today": None, "electricity_rows_tomorrow": None}],
)
def test_retained_electricity_rows_empty_without_previous_data(prev):
    assert _retained_electricity_rows(prev, date(2024, 6, 15), date(2024, 6, 16)) == ([], [])


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
