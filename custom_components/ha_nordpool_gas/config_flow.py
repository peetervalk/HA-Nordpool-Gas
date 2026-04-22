from __future__ import annotations

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.config_entries import ConfigFlowResult
from homeassistant.helpers.selector import (
    BooleanSelector,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
)

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
    DEFAULT_TRANSFER_DAY,
    DEFAULT_TRANSFER_DAY_END,
    DEFAULT_TRANSFER_DAY_START,
    DEFAULT_TRANSFER_FIXED,
    DEFAULT_TRANSFER_MODE,
    DEFAULT_TRANSFER_NIGHT,
    DEFAULT_TRANSFER_WEEKENDS_NIGHT,
    DEFAULT_VAT,
    DOMAIN,
    TRANSFER_MODE_DAY_NIGHT,
    TRANSFER_MODE_FIXED,
    TRANSFER_MODE_NONE,
)

_AREA_OPTIONS = [
    SelectOptionDict(value="ee", label="Estonia"),
    SelectOptionDict(value="lv", label="Latvia"),
    SelectOptionDict(value="fi", label="Finland"),
    SelectOptionDict(value="lt", label="Lithuania"),
]

_TRANSFER_MODE_OPTIONS = [
    SelectOptionDict(value=TRANSFER_MODE_NONE, label="None"),
    SelectOptionDict(value=TRANSFER_MODE_FIXED, label="Fixed rate"),
    SelectOptionDict(value=TRANSFER_MODE_DAY_NIGHT, label="Day / Night rates"),
]

_NUM = NumberSelectorConfig(min=0, step=0.01, mode=NumberSelectorMode.BOX)
_NUM_PCT = NumberSelectorConfig(min=0, max=100, step=0.1, mode=NumberSelectorMode.BOX)
_NUM_HOUR = NumberSelectorConfig(min=0, max=23, step=1, mode=NumberSelectorMode.BOX)


def _base_schema(defaults: dict) -> vol.Schema:
    return vol.Schema(
        {
            vol.Required(CONF_AREA, default=defaults.get(CONF_AREA, DEFAULT_AREA)): SelectSelector(
                SelectSelectorConfig(options=_AREA_OPTIONS, mode=SelectSelectorMode.DROPDOWN)
            ),
            vol.Required(CONF_VAT, default=defaults.get(CONF_VAT, DEFAULT_VAT)): NumberSelector(_NUM_PCT),
            vol.Required(CONF_GAS_EXCISE, default=defaults.get(CONF_GAS_EXCISE, DEFAULT_GAS_EXCISE)): NumberSelector(_NUM),
            vol.Required(CONF_TRANSFER_MODE, default=defaults.get(CONF_TRANSFER_MODE, DEFAULT_TRANSFER_MODE)): SelectSelector(
                SelectSelectorConfig(options=_TRANSFER_MODE_OPTIONS, mode=SelectSelectorMode.DROPDOWN)
            ),
        }
    )


def _fixed_schema(defaults: dict) -> vol.Schema:
    return vol.Schema(
        {
            vol.Required(
                CONF_TRANSFER_FIXED,
                default=defaults.get(CONF_TRANSFER_FIXED, DEFAULT_TRANSFER_FIXED),
            ): NumberSelector(_NUM),
        }
    )


def _day_night_schema(defaults: dict) -> vol.Schema:
    return vol.Schema(
        {
            vol.Required(CONF_TRANSFER_DAY, default=defaults.get(CONF_TRANSFER_DAY, DEFAULT_TRANSFER_DAY)): NumberSelector(_NUM),
            vol.Required(CONF_TRANSFER_NIGHT, default=defaults.get(CONF_TRANSFER_NIGHT, DEFAULT_TRANSFER_NIGHT)): NumberSelector(_NUM),
            vol.Required(CONF_TRANSFER_DAY_START, default=defaults.get(CONF_TRANSFER_DAY_START, DEFAULT_TRANSFER_DAY_START)): NumberSelector(_NUM_HOUR),
            vol.Required(CONF_TRANSFER_DAY_END, default=defaults.get(CONF_TRANSFER_DAY_END, DEFAULT_TRANSFER_DAY_END)): NumberSelector(_NUM_HOUR),
            vol.Required(
                CONF_TRANSFER_WEEKENDS_NIGHT,
                default=defaults.get(CONF_TRANSFER_WEEKENDS_NIGHT, DEFAULT_TRANSFER_WEEKENDS_NIGHT),
            ): BooleanSelector(),
        }
    )


class SpotPriceConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1

    def __init__(self) -> None:
        self._data: dict = {}

    async def async_step_user(self, user_input: dict | None = None) -> ConfigFlowResult:
        if user_input is not None:
            self._data.update(user_input)
            if user_input[CONF_TRANSFER_MODE] == TRANSFER_MODE_FIXED:
                return await self.async_step_transfer_fixed()
            if user_input[CONF_TRANSFER_MODE] == TRANSFER_MODE_DAY_NIGHT:
                return await self.async_step_transfer_day_night()
            return self._create_entry()
        return self.async_show_form(step_id="user", data_schema=_base_schema({}))

    async def async_step_transfer_fixed(self, user_input: dict | None = None) -> ConfigFlowResult:
        if user_input is not None:
            self._data.update(user_input)
            return self._create_entry()
        return self.async_show_form(step_id="transfer_fixed", data_schema=_fixed_schema({}))

    async def async_step_transfer_day_night(self, user_input: dict | None = None) -> ConfigFlowResult:
        if user_input is not None:
            self._data.update(user_input)
            return self._create_entry()
        return self.async_show_form(step_id="transfer_day_night", data_schema=_day_night_schema({}))

    def _create_entry(self) -> ConfigFlowResult:
        area = self._data.get(CONF_AREA, DEFAULT_AREA).upper()
        return self.async_create_entry(title=f"Spot Price ({area})", data=self._data)

    @staticmethod
    def async_get_options_flow(config_entry: config_entries.ConfigEntry) -> SpotPriceOptionsFlow:
        return SpotPriceOptionsFlow()


class SpotPriceOptionsFlow(config_entries.OptionsFlow):
    def __init__(self) -> None:
        self._data: dict = {}

    def _current(self, key: str, default):
        return self.config_entry.options.get(key, self.config_entry.data.get(key, default))

    async def async_step_init(self, user_input: dict | None = None) -> ConfigFlowResult:
        if user_input is not None:
            self._data.update(user_input)
            if user_input[CONF_TRANSFER_MODE] == TRANSFER_MODE_FIXED:
                return await self.async_step_transfer_fixed()
            if user_input[CONF_TRANSFER_MODE] == TRANSFER_MODE_DAY_NIGHT:
                return await self.async_step_transfer_day_night()
            return self.async_create_entry(title="", data=self._data)

        defaults = {
            CONF_AREA: self._current(CONF_AREA, DEFAULT_AREA),
            CONF_VAT: self._current(CONF_VAT, DEFAULT_VAT),
            CONF_GAS_EXCISE: self._current(CONF_GAS_EXCISE, DEFAULT_GAS_EXCISE),
            CONF_TRANSFER_MODE: self._current(CONF_TRANSFER_MODE, DEFAULT_TRANSFER_MODE),
        }
        return self.async_show_form(step_id="init", data_schema=_base_schema(defaults))

    async def async_step_transfer_fixed(self, user_input: dict | None = None) -> ConfigFlowResult:
        if user_input is not None:
            self._data.update(user_input)
            return self.async_create_entry(title="", data=self._data)
        defaults = {CONF_TRANSFER_FIXED: self._current(CONF_TRANSFER_FIXED, DEFAULT_TRANSFER_FIXED)}
        return self.async_show_form(step_id="transfer_fixed", data_schema=_fixed_schema(defaults))

    async def async_step_transfer_day_night(self, user_input: dict | None = None) -> ConfigFlowResult:
        if user_input is not None:
            self._data.update(user_input)
            return self.async_create_entry(title="", data=self._data)
        defaults = {
            CONF_TRANSFER_DAY: self._current(CONF_TRANSFER_DAY, DEFAULT_TRANSFER_DAY),
            CONF_TRANSFER_NIGHT: self._current(CONF_TRANSFER_NIGHT, DEFAULT_TRANSFER_NIGHT),
            CONF_TRANSFER_DAY_START: self._current(CONF_TRANSFER_DAY_START, DEFAULT_TRANSFER_DAY_START),
            CONF_TRANSFER_DAY_END: self._current(CONF_TRANSFER_DAY_END, DEFAULT_TRANSFER_DAY_END),
            CONF_TRANSFER_WEEKENDS_NIGHT: self._current(CONF_TRANSFER_WEEKENDS_NIGHT, DEFAULT_TRANSFER_WEEKENDS_NIGHT),
        }
        return self.async_show_form(step_id="transfer_day_night", data_schema=_day_night_schema(defaults))
