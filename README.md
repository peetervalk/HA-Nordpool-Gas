# Spot Price — Home Assistant Integration

Fetches electricity and gas spot prices and exposes them as Home Assistant sensor entities.

**Electricity** is fetched from the [Elering NPS price API](https://dashboard.elering.ee/api/nps/price/csv) at 15-minute resolution. **Gas** is fetched from the [EEX NGP daily CSV](https://gasandregistry.eex.com/Gas/NGP/LVA-EST_NGP_15_Mins.csv).

Both today's and tomorrow's prices are fetched on every update. Tomorrow's electricity prices are explicitly re-fetched at **16:00** (when Nord Pool prices are expected to be available in Elering).

## Entities

| Entity | State | Extra attributes |
|---|---|---|
| `Electricity Price 15min` | Current 15-min slot price (c/kWh) | `electricity_rows_today`, `electricity_rows_tomorrow`, `hourly_today`, `hourly_tomorrow`, `tomorrow_valid`, `updated_at` |
| `Electricity Price Hourly` | Average of today's four 15-min prices for the current hour (c/kWh) | `updated_at` |
| `Gas Price` | Today's gas price (c/kWh) | `gas_tomorrow`, `updated_at` |

Prices include VAT and optional transfer fees. Entities update at **:00, :15, :30 and :45** each hour.

## Installation

### HACS (recommended)

1. In Home Assistant go to **HACS → Integrations → ⋮ → Custom repositories**
2. Add `https://github.com/peetervalk/shelly-price-optimizer` as category **Integration**
3. Search for **Spot Price** and install it
4. Restart Home Assistant

### Manual

Copy `custom_components/ha_nordpool_gas/` into your HA `config/custom_components/` directory and restart.

## Configuration

Add to `configuration.yaml`:

```yaml
sensor:
  - platform: ha_nordpool_gas
```

### Full example with all options

```yaml
sensor:
  - platform: ha_nordpool_gas
    name: "Spot Price"         # Entity name prefix
    area: "ee"                 # NPS area: ee, fi, lt, lv (default: ee)
    vat: 24.0                  # VAT percentage (default: 24.0)
    day_transfer: 0.0          # Day tariff added to electricity price c/kWh (default: 0.0)
                               # Applied 07:00–22:00
    night_transfer: 0.0        # Night tariff added to electricity price c/kWh (default: 0.0)
                               # Applied 22:00–07:00
    gas_excise: 0.0            # Fixed excise added to gas price c/kWh (default: 0.0)
```

### Options

| Option | Default | Description |
|---|---|---|
| `name` | `Spot Price` | Prefix for all entity names |
| `area` | `ee` | NPS price area — `ee`, `fi`, `lt` or `lv` |
| `vat` | `24.0` | VAT % applied to both electricity and gas |
| `day_transfer` | `0.0` | Transfer fee added to electricity during 07:00–22:00 (c/kWh) |
| `night_transfer` | `0.0` | Transfer fee added to electricity during 22:00–07:00 (c/kWh) |
| `gas_excise` | `0.0` | Fixed fee added to gas price (c/kWh) |
