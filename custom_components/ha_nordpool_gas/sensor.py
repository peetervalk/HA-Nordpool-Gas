from __future__ import annotations

import asyncio
import logging
import random
import time

import aiohttp
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Callable

from pynordpool import Currency, NordPoolClient, NordPoolEmptyResponseError, NordPoolError

from homeassistant.components.sensor import SensorEntity, SensorStateClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
import homeassistant.util.dt as dt_util
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_track_time_change
from homeassistant.helpers.update_coordinator import CoordinatorEntity, DataUpdateCoordinator

from .const import (
    CONF_AREA,
    CONF_GAS_EXCISE,
    CONF_TRANSFER_DAY,
    CONF_TRANSFER_DAY_END,
    CONF_TRANSFER_DAY_START,
    CONF_TRANSFER_FIXED,
    CONF_TRANSFER_MODE,
    CONF_TRANSFER_NIGHT,
    CONF_TRANSFER_WEEKENDS_NIGHT,
    CONF_VAT,
    DEFAULT_AREA,
    DEFAULT_GAS_EXCISE,
    DEFAULT_VAT,
    DOMAIN,
    GAS_HUB_BY_AREA,
    TRANSFER_MODE_DAY_NIGHT,
    TRANSFER_MODE_FIXED,
    TRANSFER_MODE_NONE,
)
from .eexgas import EexGasClient, EexGasError, GasDayPrice

_LOGGER = logging.getLogger(__name__)

GAS_STALE_SECONDS = 3600

# Nord Pool delivery days are CET/CEST dates, the same zone core's nordpool integration uses.
NORDPOOL_TZ = dt_util.get_time_zone("Europe/Oslo")


@dataclass(frozen=True)
class PriceSensorDescription:
    key: str
    name: str
    data_key: str


SENSORS = (
    PriceSensorDescription("electricity_15min", "Electricity Price 15min", "electricity_15min"),
    PriceSensorDescription("electricity_hourly", "Electricity Price Hourly", "electricity_hourly"),
    PriceSensorDescription("gas_now", "Gas Price", "gas_now"),
)


def _delivery_dates(today: date) -> list[date]:
    """Return the Nord Pool delivery days (CET dates) that overlap local today and tomorrow.

    For Baltic and Finnish local time that is yesterday, today and tomorrow, because
    local midnight falls at 23:00 CET on the previous day.
    """
    first = dt_util.start_of_local_day(today).astimezone(NORDPOOL_TZ).date()
    window_end = dt_util.start_of_local_day(today + timedelta(days=2)) - timedelta(microseconds=1)
    last = window_end.astimezone(NORDPOOL_TZ).date()
    return [first + timedelta(days=offset) for offset in range((last - first).days + 1)]


def _make_transfer_fn(config: dict) -> Callable[[datetime], float]:
    """Return a callable that computes the network transfer fee for a naive local datetime."""
    mode = config.get(CONF_TRANSFER_MODE, TRANSFER_MODE_NONE)
    if mode == TRANSFER_MODE_FIXED:
        fee = float(config.get(CONF_TRANSFER_FIXED, 0.0))
        return lambda _dt: fee
    if mode == TRANSFER_MODE_DAY_NIGHT:
        day_rate = float(config.get(CONF_TRANSFER_DAY, 0.0))
        night_rate = float(config.get(CONF_TRANSFER_NIGHT, 0.0))
        day_start = int(config.get(CONF_TRANSFER_DAY_START, 7))
        day_end = int(config.get(CONF_TRANSFER_DAY_END, 22))
        weekends_night = bool(config.get(CONF_TRANSFER_WEEKENDS_NIGHT, True))

        def _day_night_fn(dt: datetime) -> float:
            is_weekend = dt.weekday() in (5, 6)
            is_night_hours = dt.hour >= day_end or dt.hour < day_start
            if is_night_hours or (weekends_night and is_weekend):
                return night_rate
            return day_rate

        return _day_night_fn
    return lambda _dt: 0.0


def _apply_electricity_fees(
    entries: list[tuple[datetime, float]],
    vat: float,
    transfer_fn: Callable[[datetime], float],
    today: date,
    tomorrow: date,
) -> tuple[list[tuple[datetime, float]], list[tuple[datetime, float]]]:
    """Turn Nord Pool (start, EUR/MWh) entries into (today_rows, tomorrow_rows) with fees and VAT.

    Each start is converted to HA local time via dt_util.as_local(), so rows land on
    the right local date across DST transitions. Entries outside today and tomorrow
    are dropped.
    """
    today_rows: list[tuple[datetime, float]] = []
    tomorrow_rows: list[tuple[datetime, float]] = []
    for start, price_eur_mwh in entries:
        dt = dt_util.as_local(start)
        row_date = dt.date()
        if row_date not in (today, tomorrow):
            continue
        transfer = transfer_fn(dt)
        price_final = round((price_eur_mwh + transfer) * (100 + vat) / 100, 2)
        if row_date == today:
            today_rows.append((dt, price_final))
        else:
            tomorrow_rows.append((dt, price_final))
    return today_rows, tomorrow_rows


def _build_hourly_averages(rows: list[tuple[datetime, float]]) -> dict[int, float]:
    """Return {hour: avg_price} calculated from the 15-min row tuples."""
    groups: dict[int, list[float]] = defaultdict(list)
    for dt, price in rows:
        groups[dt.hour].append(price)
    return {hour: round(sum(prices) / len(prices), 2) for hour, prices in groups.items()}


def _build_hourly_rows(rows: list[tuple[datetime, float]]) -> list[list]:
    """Return [timestamp, avg_price] lists, one per local clock hour of the 15-min row tuples.

    Hours are keyed by the epoch of their start rather than by hour number, so a
    23-hour DST day yields 23 rows and a 25-hour day 25, with the repeated hour kept apart.
    """
    groups: dict[int, list[float]] = defaultdict(list)
    for dt, price in rows:
        groups[int(dt.replace(minute=0, second=0, microsecond=0).timestamp())].append(price)
    return [[hour_start, round(sum(prices) / len(prices), 2)] for hour_start, prices in groups.items()]


def _current_slot_price(rows: list[tuple[datetime, float]], now: datetime) -> float | None:
    """Return the price of the 15-min slot containing `now`, or None if no row covers it.

    Slots are matched by epoch rather than by wall-clock hour and minute, so a row
    from another day, or from the other pass of a repeated DST hour, cannot match.
    """
    slot_start = int(now.timestamp()) // 900 * 900
    return next((price for dt, price in rows if int(dt.timestamp()) == slot_start), None)


def _current_hour_average(rows: list[tuple[datetime, float]], now: datetime) -> float | None:
    """Return the average price over the local clock hour containing `now`, or None.

    Matched by epoch like _current_slot_price. Equals _build_hourly_averages()[now.hour]
    except on the 25-hour DST day, where that dict merges both passes of the repeated hour.
    """
    hour_start = int(now.replace(minute=0, second=0, microsecond=0).timestamp())
    prices = [price for dt, price in rows if hour_start <= int(dt.timestamp()) < hour_start + 3600]
    return round(sum(prices) / len(prices), 2) if prices else None


def _select_gas_prices(
    prices: list[GasDayPrice], today: date, tomorrow: date
) -> tuple[float | None, float | None]:
    """Return (today_price, tomorrow_price) in EUR/MWh from EEX NGP rows.

    Rows are matched on their gas day. Zero prices are skipped, and when a gas day
    appears more than once the first non-zero row wins.
    """
    today_price: float | None = None
    tomorrow_price: float | None = None
    for row in prices:
        if row.price == 0:
            continue
        if row.gas_day == today and today_price is None:
            today_price = row.price
        elif row.gas_day == tomorrow and tomorrow_price is None:
            tomorrow_price = row.price
    return today_price, tomorrow_price


def _rows_to_list(rows: list[tuple[datetime, float]]) -> list[list]:
    """Convert (datetime, price) tuples to serialisable [timestamp, price] lists."""
    return [[int(dt.timestamp()), price] for dt, price in rows]


def _describe_error(err: BaseException) -> str:
    """Return err's message, or its cause's when it has none.

    pynordpool raises a bare NordPoolError from the last error once its retries run out.
    """
    if str(err):
        return str(err)
    if err.__cause__ is not None:
        return _describe_error(err.__cause__)
    return type(err).__name__


class _FetchLog:
    """Log a failing price source once when it starts failing and once when it recovers."""

    def __init__(self, source: str) -> None:
        self._source = source
        self._failing = False

    def failure(self, err: BaseException) -> None:
        if self._failing:
            _LOGGER.debug("%s fetch still failing: %s", self._source, _describe_error(err))
        else:
            _LOGGER.warning(
                "%s fetch failed, keeping the last known prices: %s", self._source, _describe_error(err)
            )
        self._failing = True

    def success(self) -> None:
        if self._failing:
            _LOGGER.info("%s fetch recovered", self._source)
        self._failing = False


class _ElectricityPrices:
    """Nord Pool day-ahead prices for one area, held per delivery day (CET date).

    A delivery day is fetched on every update until Nord Pool has published final
    prices for it, and is served from memory after that. Days no longer asked for are
    dropped. A failed fetch keeps the days already held, and because local rows are
    rebuilt from them on every update, they stay correct across local midnight.
    """

    def __init__(self, client: NordPoolClient, area: str) -> None:
        self._client = client
        self._area = area
        self._days: dict[date, list[tuple[datetime, float]]] = {}
        self._final: set[date] = set()
        self._log = _FetchLog("Electricity price")

    async def async_entries(self, days: list[date]) -> list[tuple[datetime, float]]:
        """Return (start, EUR/MWh) entries for the given delivery days, in day order."""
        for day in [day for day in self._days if day not in days]:
            del self._days[day]
            self._final.discard(day)
        to_fetch = [day for day in days if day not in self._final]
        if to_fetch:
            errors = [err for err in await asyncio.gather(*map(self._async_fetch, to_fetch)) if err]
            if errors:
                self._log.failure(errors[0])
            else:
                self._log.success()
        return [entry for day in days for entry in self._days.get(day, [])]

    async def _async_fetch(self, day: date) -> Exception | None:
        """Fetch one delivery day into memory; return the error if the fetch failed."""
        try:
            data = await self._client.async_get_delivery_period(
                datetime(day.year, day.month, day.day), Currency.EUR, [self._area]
            )
        except NordPoolEmptyResponseError:
            return None  # not published yet
        except (NordPoolError, aiohttp.ClientError, TimeoutError) as err:
            return err
        self._days[day] = [
            (entry.start, entry.entry[self._area]) for entry in data.entries if self._area in entry.entry
        ]
        if data.prices_final:
            self._final.add(day)
        return None


def _build_gas_series(
    gas_now: float | None, gas_tomorrow: float | None, today: date
) -> list[list]:
    """Return [timestamp, price] step rows at local midnight today, tomorrow and the day after.

    Tomorrow falls back to today's price until it is published. The last row repeats
    tomorrow's price so the step closes at tomorrow 24:00. Midnights come from
    dt_util.start_of_local_day() per date, so 23h/25h DST days stay aligned.
    """
    if gas_now is None:
        return []
    tomorrow_price = gas_now if gas_tomorrow is None else gas_tomorrow
    return _rows_to_list(
        [
            (dt_util.start_of_local_day(today), gas_now),
            (dt_util.start_of_local_day(today + timedelta(days=1)), tomorrow_price),
            (dt_util.start_of_local_day(today + timedelta(days=2)), tomorrow_price),
        ]
    )


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    config = {**entry.data, **entry.options}
    area = config.get(CONF_AREA, DEFAULT_AREA)
    vat = float(config.get(CONF_VAT, DEFAULT_VAT))
    gas_excise = float(config.get(CONF_GAS_EXCISE, DEFAULT_GAS_EXCISE))
    gas_hub = GAS_HUB_BY_AREA.get(area, GAS_HUB_BY_AREA["ee"])
    transfer_fn = _make_transfer_fn(config)

    session = async_get_clientsession(hass)
    electricity = _ElectricityPrices(NordPoolClient(session), area.upper())
    gas_client = EexGasClient(session)
    gas_log = _FetchLog("Gas price")

    async def _fetch_gas() -> list[GasDayPrice]:
        try:
            prices = await gas_client.async_get_prices(gas_hub)
        except EexGasError as err:
            gas_log.failure(err)
            return []
        gas_log.success()
        return prices

    async def _async_update_data():
        now = dt_util.now()
        now_naive = now.replace(tzinfo=None)
        today = now_naive.date()
        tomorrow = today + timedelta(days=1)

        prev = coordinator.data or {}

        electricity_entries, gas_prices = await asyncio.gather(
            electricity.async_entries(_delivery_dates(today)),
            _fetch_gas(),
        )

        # --- Electricity ---
        today_elec_rows, tomorrow_elec_rows = _apply_electricity_fees(
            electricity_entries, vat, transfer_fn, today, tomorrow
        )

        hourly_today, hourly_tomorrow = await asyncio.gather(
            hass.async_add_executor_job(_build_hourly_averages, today_elec_rows),
            hass.async_add_executor_job(_build_hourly_averages, tomorrow_elec_rows),
        )

        electricity_15min = _current_slot_price(today_elec_rows, now)
        electricity_hourly = _current_hour_average(today_elec_rows, now)

        # --- Gas ---
        gas_today_raw, gas_tomorrow_raw = _select_gas_prices(gas_prices, today, tomorrow)

        def _to_gas_price(raw: float | None) -> float | None:
            if raw is None:
                return None
            return round((raw + gas_excise) * (100 + vat) / 100, 2)

        gas_now_fresh = _to_gas_price(gas_today_raw)
        if gas_now_fresh is not None:
            gas_now = gas_now_fresh
            gas_fresh_at = int(now.timestamp())
        else:
            _LOGGER.debug("Gas today price unavailable; retaining previous value")
            gas_now = prev.get("gas_now")
            gas_fresh_at = prev.get("gas_fresh_at")

        gas_tomorrow = _to_gas_price(gas_tomorrow_raw)
        if gas_tomorrow is None:
            gas_tomorrow = prev.get("gas_tomorrow")

        return {
            "electricity_15min": electricity_15min,
            "electricity_hourly": electricity_hourly,
            "gas_now": gas_now,
            "gas_tomorrow": gas_tomorrow,
            "gas_series": _build_gas_series(gas_now, gas_tomorrow, today),
            "gas_fresh_at": gas_fresh_at,
            "electricity_rows_today": _rows_to_list(today_elec_rows),
            "electricity_rows_tomorrow": _rows_to_list(tomorrow_elec_rows),
            "hourly_today": hourly_today,
            "hourly_tomorrow": hourly_tomorrow,
            "hourly_rows_today": _build_hourly_rows(today_elec_rows),
            "hourly_rows_tomorrow": _build_hourly_rows(tomorrow_elec_rows),
            "tomorrow_valid": len(tomorrow_elec_rows) > 0,
            "updated_at": int(now.timestamp()),
        }

    coordinator = DataUpdateCoordinator(
        hass,
        logger=_LOGGER,
        name=f"{DOMAIN}_{entry.entry_id}",
        update_interval=None,
        update_method=_async_update_data,
    )

    async def _handle_time_update(_now=None):
        await coordinator.async_request_refresh()

    jitter_second = random.randint(5, 59)
    cancel_quarterly = async_track_time_change(
        hass, _handle_time_update, minute=[0, 15, 30, 45], second=jitter_second
    )

    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = coordinator
    @callback
    def _cleanup_entry_data() -> None:
        # Must not return a value: HA schedules any truthy return as a task.
        hass.data.get(DOMAIN, {}).pop(entry.entry_id, None)

    entry.async_on_unload(cancel_quarterly)
    entry.async_on_unload(_cleanup_entry_data)

    if not hass.services.has_service(DOMAIN, "refresh"):
        async def _handle_refresh_service(call):
            for coord in hass.data[DOMAIN].values():
                await coord.async_request_refresh()
        hass.services.async_register(DOMAIN, "refresh", _handle_refresh_service)

    await coordinator.async_refresh()

    async_add_entities(
        [SpotPriceSensor(coordinator, entry.entry_id, entry.title, description) for description in SENSORS]
    )


class SpotPriceSensor(CoordinatorEntity, SensorEntity):
    _attr_native_unit_of_measurement = "EUR/MWh"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _unrecorded_attributes = frozenset(
        {
            "electricity_rows_today",
            "electricity_rows_tomorrow",
            "gas_series",
            "hourly_rows_today",
            "hourly_rows_tomorrow",
            "hourly_today",
            "hourly_tomorrow",
        }
    )

    def __init__(
        self,
        coordinator: DataUpdateCoordinator,
        entry_id: str,
        base_name: str,
        description: PriceSensorDescription,
    ) -> None:
        super().__init__(coordinator)
        self._description = description
        self._attr_name = f"{base_name} {description.name}"
        self._attr_unique_id = f"{DOMAIN}_{entry_id}_{description.key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry_id)},
            name=base_name,
            manufacturer="Nord Pool / EEX",
        )

    @property
    def available(self) -> bool:
        if not super().available:
            return False
        if self._description.key == "gas_now":
            fresh_at = (self.coordinator.data or {}).get("gas_fresh_at")
            if fresh_at is None:
                return False
            return (time.time() - fresh_at) < GAS_STALE_SECONDS
        return True

    @property
    def native_value(self) -> float | None:
        return (self.coordinator.data or {}).get(self._description.data_key)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        data = self.coordinator.data or {}
        attrs = {"updated_at": data.get("updated_at")}
        if self._description.key == "electricity_15min":
            attrs["electricity_rows_today"] = data.get("electricity_rows_today")
            attrs["electricity_rows_tomorrow"] = data.get("electricity_rows_tomorrow")
            attrs["hourly_today"] = data.get("hourly_today")
            attrs["hourly_tomorrow"] = data.get("hourly_tomorrow")
            attrs["hourly_rows_today"] = data.get("hourly_rows_today")
            attrs["hourly_rows_tomorrow"] = data.get("hourly_rows_tomorrow")
            attrs["tomorrow_valid"] = data.get("tomorrow_valid")
        elif self._description.key == "electricity_hourly":
            attrs["hourly_today"] = data.get("hourly_today")
            attrs["hourly_tomorrow"] = data.get("hourly_tomorrow")
            attrs["hourly_rows_today"] = data.get("hourly_rows_today")
            attrs["hourly_rows_tomorrow"] = data.get("hourly_rows_tomorrow")
            attrs["tomorrow_valid"] = data.get("tomorrow_valid")
        elif self._description.key == "gas_now":
            attrs["gas_tomorrow"] = data.get("gas_tomorrow")
            attrs["gas_series"] = data.get("gas_series")
        return attrs
