from .eexgas import Hub

DOMAIN = "ha_nordpool_gas"
DEFAULT_VAT = 24.0
DEFAULT_GAS_EXCISE = 0.0
DEFAULT_AREA = "ee"

# EE and LV intentionally share the same hub — they trade on the same Baltic hub (LVA-EST)
# and publish an identical gas price.
GAS_HUB_BY_AREA: dict[str, Hub] = {
    "ee": Hub.LVA_EST,
    "lv": Hub.LVA_EST,
    "fi": Hub.FIN,
    "lt": Hub.LTU,
}

# Transfer fee modes
TRANSFER_MODE_NONE = "none"
TRANSFER_MODE_FIXED = "fixed"
TRANSFER_MODE_DAY_NIGHT = "day_night"

# Config entry keys
CONF_AREA = "area"
CONF_VAT = "vat"
CONF_GAS_EXCISE = "gas_excise"
CONF_TRANSFER_MODE = "transfer_mode"
CONF_TRANSFER_FIXED = "transfer_fixed"
CONF_TRANSFER_DAY = "transfer_day"
CONF_TRANSFER_NIGHT = "transfer_night"
CONF_TRANSFER_DAY_START = "transfer_day_start"
CONF_TRANSFER_DAY_END = "transfer_day_end"
CONF_TRANSFER_WEEKENDS_NIGHT = "transfer_weekends_night"

# Defaults
DEFAULT_TRANSFER_MODE = TRANSFER_MODE_NONE
DEFAULT_TRANSFER_FIXED = 0.0
DEFAULT_TRANSFER_DAY = 0.0
DEFAULT_TRANSFER_NIGHT = 0.0
DEFAULT_TRANSFER_DAY_START = 7
DEFAULT_TRANSFER_DAY_END = 22
DEFAULT_TRANSFER_WEEKENDS_NIGHT = True
