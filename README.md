# Nordpool & EEX Spot Prices — Home Assistant Integration

Fetches electricity and gas spot prices and exposes them as Home Assistant sensor entities.

**Electricity** day-ahead prices come from the [Nord Pool data portal](https://data.nordpoolgroup.com/auction/day-ahead/prices) at 15-minute resolution, through [pynordpool](https://github.com/gjohansson-ST/pynordpool), the library Home Assistant's built-in Nord Pool integration uses.
**Gas** is fetched from the [EEX NGP daily CSV](https://www.eex.com/en/market-data/market-data-hub/natural-gas/indices) — the correct file is selected automatically based on the configured region.

Electricity sensors switch to the new price exactly at **:00, :15, :30 and :45** each hour, from prices already fetched. Gas prices are fetched within a minute after each of those times, because EEX updates them during the day. Electricity prices for a day are fetched from 12:00 CET the day before, when Nord Pool's day-ahead auction closes, until Nord Pool has published final prices for it, then reused; tomorrow's prices are usually published around 13:00 CET. Those fetches run 1–4 minutes before each quarter-hour, ahead of the many clients that ask Nord Pool on the hour.

## Supported regions

| Region | Electricity source | Gas source |
|---|---|---|
| Estonia (`ee`) | Nord Pool EE | EEX LVA-EST NGP |
| Latvia (`lv`) | Nord Pool LV | EEX LVA-EST NGP |
| Finland (`fi`) | Nord Pool FI | EEX FIN NGP |
| Lithuania (`lt`) | Nord Pool LT | EEX LTU NGP |

## Entities

Three sensor entities are created per configured integration entry:

| Entity | State | Attributes |
|---|---|---|
| `Electricity Price 15min` | Current 15-min slot price (EUR/MWh) | `electricity_rows_today`, `electricity_rows_tomorrow`, `hourly_today`, `hourly_tomorrow`, `hourly_rows_today`, `hourly_rows_tomorrow`, `tomorrow_valid`, `updated_at` |
| `Electricity Price Hourly` | Hourly average of the four 15-min prices (EUR/MWh) | `hourly_today`, `hourly_tomorrow`, `hourly_rows_today`, `hourly_rows_tomorrow`, `tomorrow_valid`, `updated_at` |
| `Gas Price` | Today's gas price (EUR/MWh) | `gas_tomorrow`, `gas_series`, `updated_at` |

All prices include VAT and any configured network transfer fees.

`tomorrow_valid` turns true once Nord Pool has published tomorrow's electricity prices, usually around 13:00 CET. Before that, `electricity_rows_tomorrow` already holds tomorrow's first hour, because local midnight in the Baltics and Finland falls at 23:00 CET on the previous day, inside a delivery day that is already published. `gas_tomorrow` is empty until EEX has a price for tomorrow's gas day.

Hourly electricity prices come in two shapes. `hourly_today` and `hourly_tomorrow` map the hour number (0–23) to its price, which is convenient for looking up a given hour. `hourly_rows_today` and `hourly_rows_tomorrow` list `[epoch_seconds, price]` rows, one per clock hour at the start of that hour, which suits charts. On the days the clocks change, the row lists hold 23 or 25 rows. The hour-number maps have no entry for the skipped hour in spring, and in autumn their entry for the repeated hour is the average of both passes.

The Gas Price sensor's `gas_series` attribute lists `[epoch_seconds, price]` rows at local midnight today, tomorrow and the day after (tomorrow uses `gas_tomorrow`, or today's price until that is published), so step charts can draw gas across a fixed 48h window ending tomorrow 24:00.

## Installation

### HACS (recommended)

1. In Home Assistant go to **HACS → Integrations → ⋮ → Custom repositories**
2. Add `https://github.com/peetervalk/HA-Nordpool-Gas` as category **Integration**
3. Search for **Nordpool & EEX Spot Prices** and install it
4. Restart Home Assistant
5. Go to **Settings → Devices & Services → Add Integration**, search for **Nordpool & EEX Spot Prices** and follow the setup wizard

### Manual

1. Copy `custom_components/ha_nordpool_gas/` into your HA `config/custom_components/` directory
2. Restart Home Assistant
3. Go to **Settings → Devices & Services → Add Integration**, search for **Nordpool & EEX Spot Prices**

## Configuration

Setup is done through UI.

### Setup wizard — step 1: General settings

| Field | Description | Default |
|---|---|---|
| Region | Price area — Estonia, Latvia, Finland or Lithuania | Estonia |
| VAT (%) | Applied to both electricity and gas prices | 24.0 |
| Gas excise duty (EUR/MWh) | Fixed excise added to the raw gas price | 0.0 |
| Network transfer fee type | How the electricity network tariff is included — see below | None |

Add all costs excluding VAT.

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

Open **Settings → Devices & Services**, find the Nordpool & EEX Spot Prices entry and click **Configure** to update VAT, gas excise duty or transfer fee settings. HA will reload the integration automatically.

> **Region cannot be changed after setup.** To switch region, remove the existing entry and add a new one. Multiple entries (one per region) are supported simultaneously.

## Actions

### `ha_nordpool_gas.refresh`

Forces an immediate fetch of gas prices, and of any electricity prices still missing, for all Nordpool & EEX Spot Prices instances, bypassing the normal schedule. Useful after a connectivity issue or for automations that need an up-to-date value on demand.

```yaml
action: ha_nordpool_gas.refresh
```

No parameters required.

## Using prices in automations

The `electricity_rows_today` and `hourly_today` attributes on the 15-min sensor contain full day arrays, which are useful for energy automations.

> **Gas Price availability:** the gas sensor reports unavailable if no fresh price has been received for more than one hour. This can happen on days when EEX publishes late or during connectivity issues. Electricity sensors remain available and keep using the prices already fetched for today and tomorrow — electricity prices are published once per day and do not change. If fetching keeps failing until those prices run out, the electricity sensors report `unknown` until the next successful fetch.
