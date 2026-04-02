# Examples

These examples assume you have the **ha_nordpool_gas** integration installed and working,
with the following entities available:

| Entity | Description |
|--------|-------------|
| `sensor.spot_price_electricity_price_hourly` | Current hour electricity spot price (EUR/MWh) |
| `sensor.spot_price_gas_price` | Today's gas spot price (EUR/MWh) |

---

## samplegraph.yml — ApexCharts price chart

An [ApexCharts card](https://github.com/RomRider/apexcharts-card) dashboard card showing
electricity hourly prices for 48 h alongside the flat gas reference price.
Bars are colour-coded:

- 🟢 **Green** — electricity cheaper than gas (good time to run high-consumption appliances)
- 🔵 **Blue** — electricity more expensive than gas but below 400 EUR/MWh
- 🔴 **Red** — electricity above 400 EUR/MWh (very expensive)

**Requirements:** [ApexCharts card](https://github.com/RomRider/apexcharts-card) installed via HACS.

**Usage:** Copy the contents of `samplegraph.yml` into a new Manual card in your HA dashboard.

---

## boiler_automation.yaml — Smart boiler control

Three automations that control an electric boiler via a **Shelly Gen1 plug**
(MQTT) based on spot prices, with an immediate override when a sauna (Shelly Gen3)
is drawing significant current.

**Logic:**
- Boiler **ON** when electricity price < gas price
- Boiler **OFF** when electricity price ≥ gas price
- Boiler **OFF immediately** when sauna current sensor exceeds 1 A
- Boiler **back ON** after sauna finishes, if electricity is still cheaper than gas

**Before using**, edit the file and adjust these values to match your setup:

| Value | Description |
|-------|-------------|
| `shellies/shelly-boiler/relay/0/command` | MQTT topic for your Gen1 Shelly plug |
| `sensor.saun_faas2_current` | HA entity ID for your Gen3 Shelly current sensor |

**Usage:** Copy the contents into your `automations.yaml` or import via
HA → Settings → Automations → ⋮ → Import YAML.
