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

Three automations that control an electric boiler via a **Shelly Gen1 plug over HTTP**
based on spot prices, with an immediate override when a sauna (Shelly Gen3)
is drawing significant current.

**Logic:**
- Boiler **ON** when electricity price < gas price
- Boiler **OFF** when electricity price ≥ gas price
- Boiler **OFF immediately** when sauna current sensor exceeds 1 A
- Boiler **back ON** after sauna finishes, if electricity is still cheaper than gas

**Note:** `boiler_price_check` uses `max_exceeded: silent` to suppress "already running" warnings when both price sensors update close together.

### Required setup (before importing automation)

Add this to your `configuration.yaml`:

```yaml
rest_command:
  shelly_boiler_on:
    url: "http://192.168.1.177/relay/0?turn=on"
    method: get
  shelly_boiler_off:
    url: "http://192.168.1.177/relay/0?turn=off"
    method: get
```

Then restart Home Assistant (or reload YAML config for `rest_command`).

### Automation setup

1. Open `examples/boiler_automation.yaml`
2. Confirm entity IDs:
   - `sensor.saun_faas2_current`
   - `sensor.spot_price_gas_price`
   - `sensor.spot_price_electricity_price_hourly`
3. Copy content into `automations.yaml` (replace `[]` if empty list)
4. Reload automations

### Quick connectivity test

From a browser on your LAN:
- `http://192.168.1.177/relay/0?turn=on`
- `http://192.168.1.177/relay/0?turn=off`

If these URLs work, the automation control path should work as well.
