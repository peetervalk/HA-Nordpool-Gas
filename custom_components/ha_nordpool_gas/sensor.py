from __future__ import annotations

import csv
import io
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta

import voluptuous as vol

from homeassistant.components.sensor import SensorEntity
from homeassistant.const import CONF_NAME
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity, DataUpdateCoordinator

from .const import (
    DEFAULT_DAY_TRANSFER,
    DEFAULT_GAS_EXCISE,
    DEFAULT_NAME,
    DEFAULT_NIGHT_TRANSFER,
    DEFAULT_SCAN_INTERVAL_MINUTES,
    DEFAULT_VAT,
    DOMAIN,
    EEX_URL,
    ELERING_URL,
)

CONF_ELECTRICITY_URL = "electricity_url"
CONF_GAS_URL = "gas_url"
CONF_SCAN_INTERVAL = "scan_interval_minutes"
CONF_VAT = "vat"
CONF_DAY_TRANSFER = "day_transfer"
CONF_NIGHT_TRANSFER = "night_transfer"
CONF_GAS_EXCISE = "gas_excise"

PLATFORM_SCHEMA = cv.PLATFORM_SCHEMA.extend(
    {
        vol.Optional(CONF_NAME, default=DEFAULT_NAME): cv.string,
        vol.Optional(CONF_ELECTRICITY_URL, default=ELERING_URL): cv.string,
        vol.Optional(CONF_GAS_URL, default=EEX_URL): cv.string,
        vol.Optional(CONF_SCAN_INTERVAL, default=DEFAULT_SCAN_INTERVAL_MINUTES): cv.positive_int,
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
    PriceSensorDescription("electricity_now", "Electricity Price Now", "electricity_now"),
    PriceSensorDescription("gas_now", "Gas Price Now", "gas_now"),
    PriceSensorDescription("electricity_avg", "Electricity Price Average", "electricity_avg"),
    PriceSensorDescription("gas_avg", "Gas Price Average", "gas_avg"),
)


async def async_setup_platform(
    hass: HomeAssistant,
    config,
    async_add_entities: AddEntitiesCallback,
    discovery_info=None,
) -> None:
    name = config[CONF_NAME]
    electricity_url = config[CONF_ELECTRICITY_URL]
    gas_url = config[CONF_GAS_URL]
    scan_interval_minutes = config[CONF_SCAN_INTERVAL]
    vat = config[CONF_VAT]
    day_transfer = config[CONF_DAY_TRANSFER]
    night_transfer = config[CONF_NIGHT_TRANSFER]
    gas_excise = config[CONF_GAS_EXCISE]

    session = async_get_clientsession(hass)

    async def _async_update_data():
        now = datetime.now()
        electricity_hourly = []
        gas_hourly = []

        electricity_resp = await session.get(electricity_url, timeout=30)
        electricity_resp.raise_for_status()
        electricity_text = await electricity_resp.text()

        for row in csv.DictReader(io.StringIO(electricity_text)):
            try:
                price_eur_mwh = float(row.get("Price", 0))
                dt = datetime.strptime(row.get("Timestamp", ""), "%Y-%m-%d %H:%M:%S")
            except (ValueError, TypeError):
                continue

            price_c_kwh = (price_eur_mwh / 10) * (100 + vat) / 100
            if 7 <= dt.hour < 22:
                price_c_kwh += day_transfer
            else:
                price_c_kwh += night_transfer
            electricity_hourly.append([int(dt.timestamp()), round(price_c_kwh, 2)])

        gas_resp = await session.get(gas_url, timeout=30)
        gas_resp.raise_for_status()
        gas_text = await gas_resp.text()

        target_date = now.strftime("%d/%m/%Y")
        gas_price = None
        for row in csv.DictReader(io.StringIO(gas_text), delimiter=";"):
            if row.get("Date", "").strip() != target_date:
                continue
            try:
                gas_price = float(str(row.get("Price", "0")).replace(",", "."))
                break
            except (ValueError, TypeError):
                continue

        if gas_price is not None:
            gas_c_kwh = round(((gas_price / 10) * (100 + vat) / 100) + gas_excise, 2)
            day_start = datetime(now.year, now.month, now.day)
            for hour in range(24):
                ts = int((day_start + timedelta(hours=hour)).timestamp())
                gas_hourly.append([ts, gas_c_kwh])

        current_hour = int(now.replace(minute=0, second=0, microsecond=0).timestamp())
        electricity_now = next((p[1] for p in electricity_hourly if p[0] == current_hour), None)
        gas_now = next((p[1] for p in gas_hourly if p[0] == current_hour), None)

        electricity_values = [p[1] for p in electricity_hourly]
        gas_values = [p[1] for p in gas_hourly]

        return {
            "electricity_hourly": electricity_hourly,
            "gas_hourly": gas_hourly,
            "electricity_now": electricity_now,
            "gas_now": gas_now,
            "electricity_avg": round(sum(electricity_values) / len(electricity_values), 2)
            if electricity_values
            else None,
            "gas_avg": round(sum(gas_values) / len(gas_values), 2) if gas_values else None,
            "updated_at": int(now.timestamp()),
        }

    coordinator = DataUpdateCoordinator(
        hass,
        logger=_LOGGER,
        name=f"{DOMAIN}_{name}",
        update_interval=timedelta(minutes=scan_interval_minutes),
        update_method=_async_update_data,
    )

    await coordinator.async_refresh()

    async_add_entities(
        [SpotPriceSensor(coordinator, name, description) for description in SENSORS]
    )


class SpotPriceSensor(CoordinatorEntity, SensorEntity):
    _attr_native_unit_of_measurement = "c/kWh"

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
        return {
            "updated_at": self.coordinator.data.get("updated_at"),
            "electricity_hourly": self.coordinator.data.get("electricity_hourly"),
            "gas_hourly": self.coordinator.data.get("gas_hourly"),
        }
