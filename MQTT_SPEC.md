# MQTT Specification - Shelly Price Optimizer

**Complete MQTT topic and message format contract between Home Assistant and Shelly.**

This document defines the exact format for all MQTT communication. Use this as reference when debugging or integrating with other systems.

---

## Overview

| Topic | Direction | Purpose | Frequency |
|-------|-----------|---------|-----------|
| `shelly/price-optimizer/prices` | HA → Shelly | Price update (electricity + gas) | Hourly + 16:00 |
| `shelly/price-optimizer/cmd/<n>` | HA → Shelly | Relay command for instance n | On decision change |
| `shelly/price-optimizer/state/<n>` | Shelly → HA | State update (optional, for UI) | Every 5 seconds |

---

## Topic 1: Price Update

**Topic:** `shelly/price-optimizer/prices`

**Direction:** Home Assistant → Shelly

**Frequency:** Once per hour + at 16:00 (for tomorrow's prices)

**Purpose:** Send fetched and normalized price data to Shelly

### Payload Schema

```json
{
  "timestamp": 1704067200,
  "electricity": {
    "hourly": [
      [1704067200, 12.5],
      [1704070800, 13.2],
      [1704074400, 14.1]
    ],
    "stats": {
      "avg": 13.27,
      "low": 12.5,
      "high": 14.1
    }
  },
  "gas": {
    "hourly": [
      [1704067200, 25.0],
      [1704070800, 25.0],
      [1704074400, 25.0]
    ],
    "stats": {
      "avg": 25.0,
      "low": 25.0,
      "high": 25.0
    }
  },
  "tomorrow": {
    "electricity": {
      "hourly": [...],
      "stats": {...}
    },
    "gas": {
      "hourly": [...],
      "stats": {...}
    }
  }
}
```

### Field Definitions

#### Root Level
```
timestamp (number, required)
  - Unix epoch in seconds
  - When this price update was created
  - Example: 1704067200 = 2024-01-01 00:00:00 UTC
```

#### electricity / gas Structure
```
hourly (array of arrays, required)
  - Format: [[epoch, price_in_cents_per_kwh], ...]
  - Always 24 entries for a day (one per hour)
  - Epochs in UTC, hourly boundaries (00:00, 01:00, etc.)
  - Prices already include VAT, transfer costs, taxes
  - Example: [1704067200, 12.5] = price 12.5 c/kWh at 2024-01-01 00:00 UTC

stats (object, required)
  - avg: Average price for the day (float, c/kWh)
  - low: Lowest price for the day (float, c/kWh)
  - high: Highest price for the day (float, c/kWh)
  - Useful for UI display and statistics
```

#### tomorrow (object, optional)
```
- Same structure as electricity/gas above
- Provided when tomorrow's prices available
- Elering typically publishes at 14:00 or 15:00 local time
- EEX provides next day price same day after ~16:00
```

### Example: Minimal Valid Payload

```json
{
  "timestamp": 1704067200,
  "electricity": {
    "hourly": [[1704067200, 12.5]],
    "stats": {"avg": 12.5, "low": 12.5, "high": 12.5}
  },
  "gas": {
    "hourly": [[1704067200, 25.0]],
    "stats": {"avg": 25.0, "low": 25.0, "high": 25.0}
  }
}
```

### Shelly Behavior

When received, Shelly:
1. Validates JSON structure
2. Stores in memory (not persisted)
3. Updates internal price arrays
4. Updates stats
5. Sets `timeOK = true` (prices are valid)
6. Logs: "Electricity prices updated: avg=X c/kWh"
7. Triggers logic evaluation if idle

---

## Topic 2: Relay Command

**Topic:** `shelly/price-optimizer/cmd/<instance>`

**Direction:** Home Assistant → Shelly

**Frequency:** When relay state should change (typically every minute if logic changes)

**Purpose:** Tell Shelly to set relay ON or OFF

### Payload Schema

```json
{
  "timestamp": 1704067234,
  "cmd": 1,
  "reason": "price_below_limit",
  "price_now": 12.5,
  "limit": 15.0
}
```

### Field Definitions

```
timestamp (number, required)
  - Unix epoch when command was issued
  
cmd (0 or 1, required)
  - 0 = Turn relay OFF
  - 1 = Turn relay ON
  
reason (string, required)
  - Human-readable reason for this command
  - Examples:
    - "price_below_limit"
    - "price_above_limit"
    - "cheapest_hour"
    - "not_cheapest_hour"
    - "gas_mode_above_limit"
    - "gas_mode_below_limit"
    - "manual_override"
    - "test"
  
price_now (float, optional)
  - Current price (c/kWh) for reference
  - Useful for HA state/history
  
limit (float, optional)
  - Threshold price used for decision
  - Example: 15.0 means "if < 15 c/kWh then ON"
```

### Example Payloads

**Price below limit → turn ON:**
```json
{
  "timestamp": 1704067234,
  "cmd": 1,
  "reason": "price_below_limit",
  "price_now": 12.5,
  "limit": 15.0
}
```

**Price above limit → turn OFF:**
```json
{
  "timestamp": 1704067294,
  "cmd": 0,
  "reason": "price_above_limit",
  "price_now": 18.3,
  "limit": 15.0
}
```

**Cheapest hour mode:**
```json
{
  "timestamp": 1704067340,
  "cmd": 1,
  "reason": "cheapest_hour_in_period",
  "price_now": 8.2,
  "period": "07:00-23:00"
}
```

### Shelly Behavior

When received, Shelly:
1. Parses JSON
2. **Applies local safety override:** If current > threshold → force cmd=0
3. Executes relay command
4. Logs: "Relay X → ON/OFF (reason: ...)"
5. Updates internal state
6. (Optional) publishes state back to HA

### Instance Number

**Topic format:** `shelly/price-optimizer/cmd/<instance>`

Examples:
- `shelly/price-optimizer/cmd/0` ← Instance 0 (first relay)
- `shelly/price-optimizer/cmd/1` ← Instance 1 (second relay)
- `shelly/price-optimizer/cmd/2` ← Instance 2 (third relay)

Instance numbers are **0-indexed** and match Shelly configuration.

---

## Topic 3: State Update (Optional)

**Topic:** `shelly/price-optimizer/state/<instance>`

**Direction:** Shelly → Home Assistant

**Frequency:** Every 5 seconds (when relay active)

**Purpose:** Report current state for HA UI, monitoring, history

### Payload Schema

```json
{
  "timestamp": 1704067234,
  "instance": 0,
  "enabled": 1,
  "relay": {
    "cmd": 1,
    "actual": 1,
    "reason": "price_below_limit"
  },
  "prices": {
    "electricity_now": 12.5,
    "gas_now": 25.0
  }
}
```

### Field Definitions

```
timestamp (number)
  - When this state was measured

instance (number)
  - Which instance this state belongs to (0-indexed)

enabled (0 or 1)
  - Whether this instance is enabled in config

relay (object)
  - cmd: Commanded state (from last MQTT command)
  - actual: Actual relay hardware state
  - reason: Why it's in this state (from last command)

prices (object)
  - electricity_now: Current hour electricity price
  - gas_now: Current hour gas price
```

### Optional: Not Implemented Yet

This feature is **optional and for future enhancement**:
- Shelly can publish, HA can subscribe
- HA dashboard can show real-time state
- Useful for monitoring multiple devices
- Currently: Not required for basic operation

---

## Validation Rules

### Price Update Validation

**Shelly must check:**
```
✓ JSON is valid
✓ "electricity" key exists
✓ "electricity.hourly" is array of arrays
✓ "electricity.stats" is object with avg/low/high
✓ "gas" key exists (same rules)
✓ "timestamp" is number > 0
```

**If invalid:** Log error, keep old prices, don't update

### Command Validation

**Shelly must check:**
```
✓ JSON is valid
✓ "cmd" is 0 or 1
✓ "timestamp" is number
✓ Instance number is valid (0 to INST_COUNT-1)
```

**If invalid:** Log error, don't execute

---

## Data Type Notes

### Prices (Monetary Values)

All prices in **cents per kWh** (c/kWh), already including:
- ✓ VAT (typically 24% in Estonia)
- ✓ Transfer costs (day/night rates)
- ✓ Excise taxes (for gas)
- ✓ Any other fees

**Conversion example:**
- Elering API: 1200 (= 12.00 EUR/MWh)
- 1 MWh = 1000 kWh, so 12.00/1000 = 0.012 EUR/kWh
- × 100 = 1.2 c/kWh
- × (100 + 24)/100 for VAT = 1.488 c/kWh
- ≈ 1.5 c/kWh (after rounding)

### Epochs

All timestamps in **Unix epoch (seconds)**, UTC timezone:
- Example: 1704067200 = 2024-01-01 00:00:00 UTC
- Not milliseconds
- Must be hourly boundaries (00:00, 01:00, etc.)

### Instance Numbers

0-indexed integers:
- Instance 0 = first relay
- Instance 1 = second relay
- etc.

Must match Shelly configuration (`INST_COUNT`).

---

## Safety Guarantees

### Local Override (Crucial)

**Shelly applies current threshold BEFORE setting relay:**

```
if (current_sensor > threshold) {
  relay = OFF  // ALWAYS, overrides any command
  log("Overcurrent override: " + current + "A > " + threshold + "A")
}
```

**Why:**
- ✓ Hardware-level, no network latency
- ✓ Protects equipment if HA unreachable
- ✓ <1ms response time
- ✓ Cannot be overridden by HA

### Timeout Safety

**Shelly enters safe mode if no price update > 24 hours:**

```
if (epoch() - prices.timestamp > 86400) {
  prices.valid = false
  relay = OFF  // Force safe state
  log("Prices stale, entering safe mode")
}
```

### Failsafe Defaults

- Relay defaults to **OFF** if unknown state
- Empty/invalid prices = safe mode (relay OFF)
- Network outage = keep relay state, log warning
- HA crash = Shelly continues with old logic

---

## Error Handling

### What Shelly Does on Error

| Error | Action | Log |
|-------|--------|-----|
| Invalid JSON | Ignore message | "Failed to parse" |
| Missing field | Ignore message | "Missing: field_name" |
| Wrong type | Ignore message | "Expected number, got..." |
| Out of range | Clamp/ignore | "Price out of range" |
| Unknown instance | Ignore | "Invalid instance" |
| Stale prices | Enter safe mode | "Prices stale" |

### What HA Does on Error

| Error | Action | Log |
|-------|--------|-----|
| MQTT pub fails | Retry after delay | "MQTT publish failed" |
| API unreachable | Use cached prices | "API unreachable" |
| Parse error | Log and skip | "CSV parse error" |
| Invalid config | Use defaults | "Invalid config" |

---

## Performance Expectations

| Operation | Latency | Notes |
|-----------|---------|-------|
| API fetch (Elering) | 1-3 sec | HTTP GET |
| API fetch (EEX) | 1-3 sec | HTTP GET |
| CSV parse | <100 ms | Python on HA |
| Normalize | <100 ms | JSON creation |
| MQTT publish | <50 ms | Local network |
| Shelly receive | <100 ms | MQTT sub |
| Relay set | <50 ms | Hardware call |
| Total (API→Relay) | ~200-500 ms | End-to-end |

---

## Testing Payloads

Use these to test your setup:

### Test: Price Update
```bash
mosquitto_pub -h homeassistant.local -t "shelly/price-optimizer/prices" -m '{
  "timestamp": 1704067200,
  "electricity": {
    "hourly": [[1704067200, 12.5], [1704070800, 13.2]],
    "stats": {"avg": 12.5, "low": 12.5, "high": 13.2}
  },
  "gas": {
    "hourly": [[1704067200, 25.0]],
    "stats": {"avg": 25.0, "low": 25.0, "high": 25.0}
  }
}'
```

### Test: Relay Command
```bash
mosquitto_pub -h homeassistant.local -t "shelly/price-optimizer/cmd/0" -m '{
  "timestamp": 1704067234,
  "cmd": 1,
  "reason": "test",
  "price_now": 12.5,
  "limit": 15.0
}'
```

Check Shelly logs:
```bash
ssh admin@<shelly-ip>
tail -f /var/log/shelly_script.log | grep "price-optimizer"
```

---

## Version History

- **v1.0** (Current) — Initial MQTT specification
  - Topics: prices, cmd, state
  - Safety: local override, timeout, failsafe
  - Validation: strict JSON checking
  - Error handling: graceful degradation

---

## Reference Implementation

See `shelly-porssisahko-mqtt.js` for reference implementation of:
- MQTT subscription
- Payload parsing
- Safety override
- Timeout handling
- Logging

---

**Questions about this spec?** Check `ARCHITECTURE.md` for visual examples or `QUICK_REFERENCE.md` for troubleshooting.

