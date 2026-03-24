from __future__ import annotations

import asyncio
import csv
import io
import logging
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from urllib.parse import urlencode, urlparse, urlunparse

import voluptuous as vol

from homeassistant.components.sensor import SensorEntity
from homeassistant.const import CONF_NAME
from homeassistant.core import HomeAssistant
import homeassistant.util.dt as dt_util
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_track_time_change
from homeassistant.helpers.update_coordinator import CoordinatorEntity, DataUpdateCoordinator

from .const import (
    DEFAULT_AREA,
    DEFAULT_DAY_TRANSFER,
    DEFAULT_GAS_EXCISE,
    DEFAULT_NAME,
    DEFAULT_NIGHT_TRANSFER,
    DEFAULT_VAT,
    DOMAIN,
    EEX_URL,
    ELERING_URL,
)

CONF_ELECTRICITY_URL = "electricity_url"
CONF_GAS_URL = "gas_url"
CONF_AREA = "area"
CONF_VAT = "vat"
CONF_DAY_TRANSFER = "day_transfer"
CONF_NIGHT_TRANSFER = "night_transfer"
CONF_GAS_EXCISE = "gas_excise"

_AREAS = ["ee", "fi", "lt", "lv"]

PLATFORM_SCHEMA = cv.PLATFORM_SCHEMA.extend(
    {
        vol.Optional(CONF_NAME, default=DEFAULT_NAME): cv.string,
        vol.Optional(CONF_ELECTRICITY_URL, default=ELERING_URL): cv.string,
        vol.Optional(CONF_GAS_URL, default=EEX_URL): cv.string,
        vol.Optional(CONF_AREA, default=DEFAULT_AREA): vol.In(_AREAS),
        vol.Optional(CONF_VAT, default=DEFAULT_VAT): vol.Coerce(float),
        vol.Optional(CONF_DAY_TRANSFER, default=DEFAULT_DAY_TRANSFER): vol.Coerce(float),
        vol.Optional(CONF_NIGHT_TRANSFER, default=DEFAULT_NIGHT_TRANSFER): vol.Coerce(float),
        vol.Optional(CONF_GAS_EXCISE, default=DEFAULT_GAS_EXCISE): vol.Coerce(float),
    }
)

_LOGGER = logging.getLogger(__name__)


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


def _build_elering_url(base_url: str, area: str, today: date, tomorrow: date) -> str:
    """Append date range and area field params to the Elering CSV base URL."""
    parsed = urlparse(base_url)
    query = urlencode({"start": f"{today}T00:00:00Z", "end": f"{tomorrow}T23:59:59Z", "fields": area})
    return urlunparse(parsed._replace(query=query))


def _parse_electricity_csv(
    text: str,
    vat: float,
    day_transfer: float,
    night_transfer: float,
    today: date,
    tomorrow: date,
    utc_offset: timedelta,
) -> tuple[list[tuple[datetime, float]], list[tuple[datetime, float]]]:
    """Parse Elering CSV (semicolon-delimited), return (today_rows, tomorrow_rows).

    Columns: 'Ajatempel (UTC)' (Unix epoch UTC), 'Kuupäev (Eesti aeg)' (local datetime string),
    and a price column whose name varies by area (e.g. 'NPS Eesti') — always the last column.
    Prices are in EUR/MWh with comma as the decimal separator.
    Timestamps are converted from UTC epoch to local system time.
    """
    today_rows: list[tuple[datetime, float]] = []
    tomorrow_rows: list[tuple[datetime, float]] = []
    reader = csv.DictReader(io.StringIO(text), delimiter=";")
    price_col = reader.fieldnames[-1] if reader.fieldnames else None
    if not price_col:
        return today_rows, tomorrow_rows
    for row in reader:
        try:
            dt = datetime.utcfromtimestamp(int(row["Ajatempel (UTC)"])) + utc_offset
            price_eur_mwh = float(row[price_col].replace(",", "."))
        except (KeyError, ValueError, TypeError, OSError):
            continue
        row_date = dt.date()
        if row_date not in (today, tomorrow):
            continue
        price_final = price_eur_mwh * (100 + vat) / 100
        is_night_transfer = dt.hour >= 22 or dt.hour < 7 or dt.weekday() in (5, 6)
        if is_night_transfer:
            price_final += night_transfer
        else:
            price_final += day_transfer
        item = (dt, round(price_final, 2))
        if row_date == today:
            today_rows.append(item)
        else:
            tomorrow_rows.append(item)
    return today_rows, tomorrow_rows


def _build_hourly_averages(rows: list[tuple[datetime, float]]) -> dict[int, float]:
    """Return {hour: avg_price} calculated from the 15-min row tuples."""
    groups: dict[int, list[float]] = defaultdict(list)
    for dt, price in rows:
        groups[dt.hour].append(price)
    return {hour: round(sum(prices) / len(prices), 2) for hour, prices in groups.items()}


def _parse_gas_csv(
    text: str, today_str: str, tomorrow_str: str
) -> tuple[float | None, float | None]:
    """Parse EEX CSV and return (today_price, tomorrow_price) in EUR/MWh."""
    today_price: float | None = None
    tomorrow_price: float | None = None
    for row in csv.DictReader(io.StringIO(text), delimiter=";"):
        row_date = row.get("Date", "").strip()
        if row_date not in (today_str, tomorrow_str):
            continue
        try:
            price = float(str(row.get("Price", "0")).replace(",", "."))
        except (ValueError, TypeError):
            continue
        if row_date == today_str and today_price is None:
            today_price = price
        elif row_date == tomorrow_str and tomorrow_price is None:
            tomorrow_price = price
        if today_price is not None and tomorrow_price is not None:
            break
    return today_price, tomorrow_price


def _rows_to_list(rows: list[tuple[datetime, float]]) -> list[list]:
    """Convert (datetime, price) tuples to serialisable [timestamp, price] lists."""
    return [[int(dt.timestamp()), price] for dt, price in rows]


async def async_setup_platform(
    hass: HomeAssistant,
    config,
    async_add_entities: AddEntitiesCallback,
    discovery_info=None,
) -> None:
    name = config[CONF_NAME]
    electricity_url = config[CONF_ELECTRICITY_URL]
    gas_url = config[CONF_GAS_URL]
    area = config[CONF_AREA]
    vat = config[CONF_VAT]
    day_transfer = config[CONF_DAY_TRANSFER]
    night_transfer = config[CONF_NIGHT_TRANSFER]
    gas_excise = config[CONF_GAS_EXCISE]

    session = async_get_clientsession(hass)

    async def _fetch(url: str, verify_ssl: bool = True) -> str:
        try:
            resp = await session.get(url, timeout=30, ssl=None if verify_ssl else False)
            resp.raise_for_status()
            return await resp.text()
        except Exception as err:
            _LOGGER.error("Error fetching %s: %s", url, err)
            return ""

    async def _async_update_data():
        now = dt_util.now()
        utc_offset = now.utcoffset()
        now_naive = now.replace(tzinfo=None)
        today = now_naive.date()
        tomorrow = today + timedelta(days=1)
        today_str = now_naive.strftime("%d/%m/%Y")
        tomorrow_str = (now_naive + timedelta(days=1)).strftime("%d/%m/%Y")

        elering_url = _build_elering_url(electricity_url, area, today, tomorrow)

        electricity_text, gas_text = await asyncio.gather(
            _fetch(elering_url),
            _fetch(gas_url, verify_ssl=False),
        )

        (today_elec_rows, tomorrow_elec_rows), (gas_today_raw, gas_tomorrow_raw) = (
            await asyncio.gather(
                hass.async_add_executor_job(
                    _parse_electricity_csv,
                    electricity_text,
                    vat,
                    day_transfer,
                    night_transfer,
                    today,
                    tomorrow,
                    utc_offset,
                ),
                hass.async_add_executor_job(_parse_gas_csv, gas_text, today_str, tomorrow_str),
            )
        )

        hourly_today, hourly_tomorrow = await asyncio.gather(
            hass.async_add_executor_job(_build_hourly_averages, today_elec_rows),
            hass.async_add_executor_job(_build_hourly_averages, tomorrow_elec_rows),
        )

        block_minute = (now_naive.minute // 15) * 15
        electricity_15min = next(
            (
                price
                for dt, price in today_elec_rows
                if dt.hour == now_naive.hour and dt.minute == block_minute
            ),
            None,
        )

        electricity_hourly = hourly_today.get(now_naive.hour)

        def _to_gas_price(raw: float | None) -> float | None:
            if raw is None:
                return None
            return round((raw * (100 + vat) / 100) + gas_excise, 2)

        return {
            "electricity_15min": electricity_15min,
            "electricity_hourly": electricity_hourly,
            "gas_now": _to_gas_price(gas_today_raw),
            "gas_tomorrow": _to_gas_price(gas_tomorrow_raw),
            "electricity_rows_today": _rows_to_list(today_elec_rows),
            "electricity_rows_tomorrow": _rows_to_list(tomorrow_elec_rows),
            "hourly_today": hourly_today,
            "hourly_tomorrow": hourly_tomorrow,
            "tomorrow_valid": len(tomorrow_elec_rows) > 0,
            "updated_at": int(now.timestamp()),
        }

    coordinator = DataUpdateCoordinator(
        hass,
        logger=_LOGGER,
        name=f"{DOMAIN}_{name}",
        update_interval=None,
        update_method=_async_update_data,
    )

    async def _handle_time_update(_now=None):
        await coordinator.async_request_refresh()

    cancel_quarterly = async_track_time_change(
        hass, _handle_time_update, minute=[0, 15, 30, 45], second=5
    )
    cancel_new_prices = async_track_time_change(
        hass, _handle_time_update, hour=16, minute=0, second=5
    )
    hass.data.setdefault(DOMAIN, []).extend([cancel_quarterly, cancel_new_prices])

    await coordinator.async_refresh()

    async_add_entities(
        [SpotPriceSensor(coordinator, name, description) for description in SENSORS]
    )


class SpotPriceSensor(CoordinatorEntity, SensorEntity):
    _attr_native_unit_of_measurement = "EUR/MWh"
    _unrecorded_attributes = frozenset(
        {
            "electricity_rows_today",
            "electricity_rows_tomorrow",
            "hourly_today",
            "hourly_tomorrow",
        }
    )

    def __init__(
        self,
        coordinator: DataUpdateCoordinator,
        base_name: str,
        description: PriceSensorDescription,
    ) -> None:
        super().__init__(coordinator)
        self._description = description
        self._attr_name = f"{base_name} {description.name}"
        self._attr_unique_id = f"{DOMAIN}_{base_name.lower().replace(' ', '_')}_{description.key}"

    @property
    def native_value(self):
        return self.coordinator.data.get(self._description.data_key)

    @property
    def extra_state_attributes(self):
        data = self.coordinator.data
        attrs = {"updated_at": data.get("updated_at")}
        if self._description.key == "electricity_15min":
            attrs["electricity_rows_today"] = data.get("electricity_rows_today")
            attrs["electricity_rows_tomorrow"] = data.get("electricity_rows_tomorrow")
            attrs["hourly_today"] = data.get("hourly_today")
            attrs["hourly_tomorrow"] = data.get("hourly_tomorrow")
            attrs["tomorrow_valid"] = data.get("tomorrow_valid")
        elif self._description.key == "gas_now":
            attrs["gas_tomorrow"] = data.get("gas_tomorrow")
        return attrs
