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
from .eexgas import EexGasClient, EexGasError, GasDayPrice, Hub

_LOGGER = logging.getLogger(__name__)

GAS_STALE_SECONDS = 3600
QUARTER_MINUTES = [0, 15, 30, 45]

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


def _delivery_dates(first_day: date, last_day: date) -> list[date]:
    """Return the Nord Pool delivery days (CET dates) that overlap local days first_day..last_day.

    In Baltic and Finnish local time, local midnight falls at 23:00 CET on the previous
    day, so local today and tomorrow need yesterday's, today's and tomorrow's CET days.
    """
    first = dt_util.start_of_local_day(first_day).astimezone(NORDPOOL_TZ).date()
    window_end = dt_util.start_of_local_day(last_day + timedelta(days=1)) - timedelta(microseconds=1)
    last = window_end.astimezone(NORDPOOL_TZ).date()
    return [first + timedelta(days=offset) for offset in range((last - first).days + 1)]


def _auction_close(day: date) -> datetime:
    """Return when Nord Pool's day-ahead auction for delivery day `day` closes: 12:00 CET the day before.

    No prices for the day exist before then, whatever the local time zone.
    """
    eve = day - timedelta(days=1)
    return datetime(eve.year, eve.month, eve.day, 12, tzinfo=NORDPOOL_TZ)


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


def _gas_prices_by_day(prices: list[GasDayPrice]) -> dict[date, float]:
    """Return {gas_day: EUR/MWh} from EEX NGP rows.

    Zero prices are skipped: EEX lists the next gas day at 0 until it has been traded.
    When a gas day appears more than once the first non-zero row wins.
    """
    by_day: dict[date, float] = {}
    for row in prices:
        if row.price != 0:
            by_day.setdefault(row.gas_day, row.price)
    return by_day


def _rows_to_list(rows: list[tuple[datetime, float]]) -> list[list]:
    """Convert (datetime, price) tuples to serialisable [timestamp, price] lists."""
    return [[int(dt.timestamp()), price] for dt, price in rows]


def _describe_error(err: BaseException) -> str:
    """Return err's message, or its cause's when it has none.

    pynordpool raises a bare NordPoolError from the last error once its retries run out.
    Without any message the type name is returned, skipping the CancelledError that
    aiohttp chains under the bare TimeoutError of a timed-out request.
    """
    if str(err):
        return str(err)
    cause = err.__cause__
    if cause is not None and (str(cause) or not isinstance(cause, asyncio.CancelledError)):
        return _describe_error(cause)
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

    A delivery day is due for fetching from the close of its auction until Nord Pool
    has published final prices for it, and is served from memory after that. Days no
    longer asked for are dropped. A failed fetch keeps the days already held, and
    because local rows are rebuilt from them on every update, they stay correct across
    local midnight.
    """

    def __init__(self, client: NordPoolClient, area: str) -> None:
        self._client = client
        self._area = area
        self._days: dict[date, list[tuple[datetime, float]]] = {}
        self._final: set[date] = set()
        self._log = _FetchLog("Electricity price")

    async def async_update(self, days: list[date], now: datetime) -> bool:
        """Drop days not in `days`, fetch the ones due at `now`, and return whether the held prices changed."""
        for day in [day for day in self._days if day not in days]:
            del self._days[day]
            self._final.discard(day)
        to_fetch = [day for day in days if day not in self._final and now >= _auction_close(day)]
        if not to_fetch:
            return False
        held = dict(self._days)
        errors = [err for err in await asyncio.gather(*map(self._async_fetch, to_fetch)) if err]
        if errors:
            self._log.failure(errors[0])
        else:
            self._log.success()
        return self._days != held

    def entries(self, days: list[date]) -> list[tuple[datetime, float]]:
        """Return the (start, EUR/MWh) entries held for the given delivery days, in day order."""
        return [entry for day in days for entry in self._days.get(day, [])]

    def holds(self, days: list[date]) -> bool:
        """Return whether prices, final or preliminary, are held for every one of these days."""
        return all(self._days.get(day) for day in days)

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


class _GasPrices:
    """EEX gas prices for one hub, remembered per gas day with the time each was fetched.

    Each successful fetch refreshes every gas day the file has a price for. A failed
    fetch, or a day the file does not price, keeps what was remembered. Prices are looked
    up by gas day, so at local midnight tomorrow's price becomes today's and is never
    shown for the day after. Days before today are dropped.
    """

    def __init__(self, client: EexGasClient, hub: Hub) -> None:
        self._client = client
        self._hub = hub
        self._days: dict[date, tuple[float, int]] = {}  # gas day -> (EUR/MWh, fetched at)
        self._log = _FetchLog("Gas price")

    async def async_update(self, today: date, now_ts: int) -> None:
        """Fetch the hub's file and remember its prices for today onwards."""
        for day in [day for day in self._days if day < today]:
            del self._days[day]
        try:
            rows = await self._client.async_get_prices(self._hub)
        except EexGasError as err:
            self._log.failure(err)
            return
        self._log.success()
        for day, price in _gas_prices_by_day(rows).items():
            if day >= today:
                self._days[day] = (price, now_ts)

    def get(self, day: date) -> tuple[float, int] | None:
        """Return (EUR/MWh, fetched at in epoch seconds) for a gas day, or None if unknown."""
        return self._days.get(day)


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
    gas = _GasPrices(EexGasClient(session), gas_hub)

    async def _async_update_electricity():
        """Build the electricity sensors' data from the prices held, without fetching."""
        now = dt_util.now()
        today = now.date()
        tomorrow = today + timedelta(days=1)

        today_elec_rows, tomorrow_elec_rows = _apply_electricity_fees(
            electricity.entries(_delivery_dates(today, tomorrow)), vat, transfer_fn, today, tomorrow
        )

        hourly_today, hourly_tomorrow = await asyncio.gather(
            hass.async_add_executor_job(_build_hourly_averages, today_elec_rows),
            hass.async_add_executor_job(_build_hourly_averages, tomorrow_elec_rows),
        )

        return {
            "electricity_15min": _current_slot_price(today_elec_rows, now),
            "electricity_hourly": _current_hour_average(today_elec_rows, now),
            "electricity_rows_today": _rows_to_list(today_elec_rows),
            "electricity_rows_tomorrow": _rows_to_list(tomorrow_elec_rows),
            "hourly_today": hourly_today,
            "hourly_tomorrow": hourly_tomorrow,
            "hourly_rows_today": _build_hourly_rows(today_elec_rows),
            "hourly_rows_tomorrow": _build_hourly_rows(tomorrow_elec_rows),
            "tomorrow_valid": electricity.holds(_delivery_dates(tomorrow, tomorrow)),
            "updated_at": int(now.timestamp()),
        }

    async def _async_update_gas():
        """Fetch the EEX gas prices and build the gas sensor's data."""
        now = dt_util.now()
        today = now.date()
        tomorrow = today + timedelta(days=1)

        await gas.async_update(today, int(now.timestamp()))

        def _to_gas_price(raw: float) -> float:
            return round((raw + gas_excise) * (100 + vat) / 100, 2)

        gas_today = gas.get(today)
        gas_next = gas.get(tomorrow)
        gas_now = _to_gas_price(gas_today[0]) if gas_today else None
        gas_fresh_at = gas_today[1] if gas_today else None
        gas_tomorrow = _to_gas_price(gas_next[0]) if gas_next else None

        return {
            "gas_now": gas_now,
            "gas_tomorrow": gas_tomorrow,
            "gas_series": _build_gas_series(gas_now, gas_tomorrow, today),
            "gas_fresh_at": gas_fresh_at,
            "updated_at": int(now.timestamp()),
        }

    electricity_coordinator = DataUpdateCoordinator(
        hass,
        logger=_LOGGER,
        name=f"{DOMAIN}_{entry.entry_id}_electricity",
        update_interval=None,
        update_method=_async_update_electricity,
    )
    gas_coordinator = DataUpdateCoordinator(
        hass,
        logger=_LOGGER,
        name=f"{DOMAIN}_{entry.entry_id}_gas",
        update_interval=None,
        update_method=_async_update_gas,
    )

    async def _async_fetch_electricity() -> bool:
        """Fetch the Nord Pool days due now; return whether the prices held changed."""
        now = dt_util.now()
        today = now.date()
        try:
            return await electricity.async_update(_delivery_dates(today, today + timedelta(days=1)), now)
        except Exception:  # Runs outside the coordinator, which used to catch this; setup must not fail on it.
            _LOGGER.exception("Unexpected error fetching electricity prices")
            return False

    async def _async_refresh() -> None:
        """Fetch whatever is due now and rebuild every sensor's data."""
        await asyncio.gather(_async_fetch_electricity(), gas_coordinator.async_refresh())
        await electricity_coordinator.async_refresh()

    async def _handle_quarter(_now=None):
        # A new 15-min slot has started: switch to its price from the prices held.
        await electricity_coordinator.async_refresh()

    async def _handle_electricity_fetch(_now=None):
        if await _async_fetch_electricity():
            await electricity_coordinator.async_refresh()

    async def _handle_gas_update(_now=None):
        await gas_coordinator.async_request_refresh()

    # Nord Pool is fetched 1-4 minutes before each quarter-hour, ahead of the clients
    # that all ask it on the hour, and gas 5-59 seconds after each quarter-hour.
    fetch_at = random.randint(11 * 60, 14 * 60)  # seconds into the quarter
    gas_second = random.randint(5, 59)
    for cancel in (
        async_track_time_change(hass, _handle_quarter, minute=QUARTER_MINUTES, second=0),
        async_track_time_change(
            hass,
            _handle_electricity_fetch,
            minute=[minute + fetch_at // 60 for minute in QUARTER_MINUTES],
            second=fetch_at % 60,
        ),
        async_track_time_change(hass, _handle_gas_update, minute=QUARTER_MINUTES, second=gas_second),
    ):
        entry.async_on_unload(cancel)

    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = _async_refresh
    @callback
    def _cleanup_entry_data() -> None:
        # Must not return a value: HA schedules any truthy return as a task.
        hass.data.get(DOMAIN, {}).pop(entry.entry_id, None)

    entry.async_on_unload(_cleanup_entry_data)

    if not hass.services.has_service(DOMAIN, "refresh"):
        async def _handle_refresh_service(call):
            for refresh in hass.data[DOMAIN].values():
                await refresh()
        hass.services.async_register(DOMAIN, "refresh", _handle_refresh_service)

    await _async_refresh()

    async_add_entities(
        [
            SpotPriceSensor(
                gas_coordinator if description.key == "gas_now" else electricity_coordinator,
                entry.entry_id,
                entry.title,
                description,
            )
            for description in SENSORS
        ]
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
