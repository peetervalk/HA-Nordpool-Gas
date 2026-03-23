#!/usr/bin/env python3
"""
ha_nordpool_gas_prices.py

Home Assistant Python script to fetch and normalize electricity + gas prices.

This script:
1. Fetches CSV from Elering (electricity, hourly)
2. Fetches CSV from EEX (gas, single daily value)
3. Normalizes both into unified hourly structure
4. Stores in HA state for MQTT publishing

Usage: Call from HA automation via shell_command or REST call
Example: python3 /config/python_scripts/ha_nordpool_gas_prices.py
"""

import json
import csv
import io
from datetime import datetime, timedelta
from typing import List, Dict, Tuple, Optional
import requests
import logging

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("spot_price")

CONFIG = {
    "VAT": 24,
    "DAY_TRANSFER": 0,
    "NIGHT_TRANSFER": 0,
    "GAS_EXCISE": 0,
    "ELERING_URL": "https://dashboard.elering.ee/api/nps/price/csv",
    "EEX_URL": "https://gasandregistry.eex.com/Gas/NGP/LVA-EST_NGP_15_Mins.csv",
    "TIMEOUT": 30,
}


class PriceFetcher:
    """Fetches electricity and gas prices from APIs."""

    @staticmethod
    def fetch_csv(url: str) -> Optional[str]:
        try:
            response = requests.get(url, timeout=CONFIG["TIMEOUT"])
            response.raise_for_status()

            content = response.text
            if content.startswith("JVB"):
                import base64
                import gzip

                content = gzip.decompress(base64.b64decode(content)).decode("utf-8")

            return content
        except requests.RequestException as e:
            log.error(f"Failed to fetch {url}: {e}")
            return None


class ElectricityParser:
    """Parses Elering CSV format and normalizes to hourly array."""

    @staticmethod
    def parse(csv_text: str) -> Optional[Tuple[List, Dict]]:
        try:
            prices = []
            reader = csv.DictReader(io.StringIO(csv_text))

            for row in reader:
                try:
                    price_eur_mwh = float(row.get("Price", 0))
                    timestamp_str = row.get("Timestamp", "")

                    dt = datetime.strptime(timestamp_str, "%Y-%m-%d %H:%M:%S")
                    epoch = int(dt.timestamp())

                    price_c_kwh = (price_eur_mwh / 10) * (100 + CONFIG["VAT"]) / 100

                    hour = dt.hour
                    if 7 <= hour < 22:
                        price_c_kwh += CONFIG["DAY_TRANSFER"]
                    else:
                        price_c_kwh += CONFIG["NIGHT_TRANSFER"]

                    prices.append([epoch, round(price_c_kwh, 2)])

                except (KeyError, ValueError) as e:
                    log.warning(f"Skipped row in Elering CSV: {e}")
                    continue

            if not prices:
                raise ValueError("No prices parsed from Elering CSV")

            price_values = [p[1] for p in prices]
            stats = {
                "avg": round(sum(price_values) / len(price_values), 2),
                "low": round(min(price_values), 2),
                "high": round(max(price_values), 2),
            }

            log.info(f"Parsed {len(prices)} electricity prices (avg: {stats['avg']} c/kWh)")
            return prices, stats

        except Exception as e:
            log.error(f"Failed to parse Elering CSV: {e}")
            return None


class GasParser:
    """Parses EEX CSV format and normalizes to hourly array (24 identical entries)."""

    @staticmethod
    def parse(csv_text: str, date: Optional[datetime] = None) -> Optional[Tuple[List, Dict]]:
        try:
            if date is None:
                date = datetime.now()

            reader = csv.DictReader(io.StringIO(csv_text), delimiter=";")
            daily_price = None
            target_date_str = date.strftime("%d/%m/%Y")

            for row in reader:
                try:
                    row_date = row.get("Date", "").strip()
                    if row_date == target_date_str:
                        price_str = row.get("Price", "0").replace(",", ".")
                        daily_price = float(price_str)
                        break
                except (ValueError, KeyError) as e:
                    log.warning(f"Skipped row in EEX CSV: {e}")
                    continue

            if daily_price is None:
                raise ValueError(f"No gas price found for date {target_date_str}")

            price_c_kwh = (daily_price / 10) * (100 + CONFIG["VAT"]) / 100
            price_c_kwh += CONFIG["GAS_EXCISE"]
            price_c_kwh = round(price_c_kwh, 2)

            day_start_epoch = int(datetime(date.year, date.month, date.day).timestamp())
            prices = []
            for hour in range(24):
                epoch = day_start_epoch + (hour * 3600)
                prices.append([epoch, price_c_kwh])

            stats = {
                "avg": price_c_kwh,
                "low": price_c_kwh,
                "high": price_c_kwh,
            }

            log.info(f"Parsed gas price: {price_c_kwh} c/kWh for {target_date_str}")
            return prices, stats

        except Exception as e:
            log.error(f"Failed to parse EEX CSV: {e}")
            return None


def main():
    log.info("Starting spot price fetch and normalization...")

    fetcher = ElectricityParser()
    fetcher = PriceFetcher()

    result = {
        "timestamp": int(datetime.now().timestamp()),
        "electricity": {"hourly": [], "stats": {"avg": 0, "low": 0, "high": 0}},
        "gas": {"hourly": [], "stats": {"avg": 0, "low": 0, "high": 0}},
        "tomorrow": {
            "electricity": {"hourly": [], "stats": {"avg": 0, "low": 0, "high": 0}},
            "gas": {"hourly": [], "stats": {"avg": 0, "low": 0, "high": 0}},
        },
    }

    log.info("Fetching electricity prices from Elering...")
    elec_csv = fetcher.fetch_csv(CONFIG["ELERING_URL"])
    if elec_csv:
        parsed = ElectricityParser.parse(elec_csv)
        if parsed:
            result["electricity"]["hourly"], result["electricity"]["stats"] = parsed
    else:
        log.error("Failed to fetch electricity prices")

    log.info("Fetching gas prices from EEX...")
    gas_csv = fetcher.fetch_csv(CONFIG["EEX_URL"])
    if gas_csv:
        parsed_today = GasParser.parse(gas_csv, datetime.now())
        if parsed_today:
            result["gas"]["hourly"], result["gas"]["stats"] = parsed_today

        tomorrow = datetime.now() + timedelta(days=1)
        parsed_tomorrow = GasParser.parse(gas_csv, tomorrow)
        if parsed_tomorrow:
            result["tomorrow"]["gas"]["hourly"], result["tomorrow"]["gas"]["stats"] = parsed_tomorrow
    else:
        log.error("Failed to fetch gas prices")

    output = json.dumps(result, indent=2)
    print(output)
    log.info("Spot price fetch and normalization complete")

    return result


if __name__ == "__main__":
    main()
