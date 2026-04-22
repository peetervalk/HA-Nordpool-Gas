# Spot Price — Home Assistant Integration

Fetches electricity and gas spot prices and exposes them as Home Assistant sensor entities.

**Electricity** is fetched from the [Elering NPS price API](https://dashboard.elering.ee/api/nps/price/csv) at 15-minute resolution.
**Gas** is fetched from the [EEX NGP daily CSV](https://gasandregistry.eex.com/Gas/NGP/) — the correct file is selected automatically based on the configured region.

Both today's and tomorrow's prices are fetched on every update. Sensors refresh at **:00, :15, :30 and :45** each hour.

## Supported regions

| Region | Electricity source | Gas source |
|---|---|---|
| Estonia (`ee`) | Elering NPS | EEX LVA-EST NGP |
| Latvia (`lv`) | Elering NPS | EEX LVA-EST NGP |
| Finland (`fi`) | Elering NPS | EEX FIN NGP |
| Lithuania (`lt`) | Elering NPS | EEX LTU NGP |

## Entities

Three sensor entities are created per configured integration entry:

| Entity | State | Notable attributes |
|---|---|---|
| `<name> Electricity Price 15min` | Current 15-min slot price (EUR/MWh) | `electricity_rows_today`, `electricity_rows_tomorrow`, `hourly_today`, `hourly_tomorrow`, `tomorrow_valid`, `updated_at` |
| `<name> Electricity Price Hourly` | Hourly average of the four 15-min prices (EUR/MWh) | `hourly_today`, `hourly_tomorrow`, `tomorrow_valid`, `updated_at` |
| `<name> Gas Price` | Today's gas price (EUR/MWh) | `gas_tomorrow`, `updated_at` |

All prices include VAT and any configured network transfer fees.

## Installation

### HACS (recommended)

1. In Home Assistant go to **HACS → Integrations → ⋮ → Custom repositories**
2. Add `https://github.com/peetervalk/HA-Nordpool-Gas` as category **Integration**
3. Search for **Spot Price** and install it
4. Restart Home Assistant
5. Go to **Settings → Devices & Services → Add Integration**, search for **Spot Price** and follow the setup wizard

### Manual

1. Copy `custom_components/ha_nordpool_gas/` into your HA `config/custom_components/` directory
2. Restart Home Assistant
3. Go to **Settings → Devices & Services → Add Integration**, search for **Spot Price**

## Configuration

Setup is done entirely through the UI — no `configuration.yaml` editing required.

### Setup wizard — step 1: General settings

| Field | Description | Default |
|---|---|---|
| Region | Price area — Estonia, Latvia, Finland or Lithuania | Estonia |
| VAT (%) | Applied to both electricity and gas prices | 24.0 |
| Gas excise duty (EUR/MWh) | Fixed excise added to the raw gas price | 0.0 |
| Network transfer fee type | How the electricity network tariff is included — see below | None |

### Setup wizard — step 2: Transfer fees (conditional)

Selecting **None** skips this step. The other two modes add a second page:

**Fixed rate** — one fee applied to all hours:

| Field | Description |
|---|---|
| Transfer fee (EUR/MWh) | Added to every electricity price slot |

**Day / Night rates** — two fees with configurable hours:

| Field | Description | Default |
|---|---|---|
| Day rate (EUR/MWh) | Applied during day hours on weekdays | 0.0 |
| Night rate (EUR/MWh) | Applied outside day hours and optionally weekends | 0.0 |
| Day starts at (hour 0–23) | First hour of the day tariff window | 7 |
| Day ends at (hour 0–23) | First hour after the day tariff window | 22 |
| Weekends always use night rate | Saturday and Sunday always billed at night rate | Yes |

> Example for a typical Estonian Elering tariff: day rate = your day tariff, night rate = your night tariff, day start = 7, day end = 22, weekends night = on.

### Reconfiguring after installation

Open **Settings → Devices & Services**, find the Spot Price entry and click **Configure** to change any of the above settings. HA will reload the integration automatically.

## Actions

### `ha_nordpool_gas.refresh`

Forces an immediate data fetch for all Spot Price instances, bypassing the normal 15-minute schedule. Useful after a connectivity issue or for automations that need an up-to-date value on demand.

```yaml
action: ha_nordpool_gas.refresh
```

No parameters required.

## Using prices in automations

The `electricity_rows_today` and `hourly_today` attributes on the 15-min sensor contain full day arrays, which are useful for energy automations. See the [examples/](examples/) folder for a boiler automation and a sample energy graph.
