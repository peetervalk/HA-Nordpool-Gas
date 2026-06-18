"""
Pytest configuration for layer-1 (pure function) tests.

We mock all homeassistant.* imports so the test suite runs without a full HA
installation and without any Unix-only modules (fcntl, etc.).
The only HA piece that needs real behaviour is homeassistant.util.dt — we
provide a faithful implementation backed by the stdlib zoneinfo module.
All sys.modules entries must be set at module level (before any test file
imports sensor.py during collection).
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

import pytest


# ---------------------------------------------------------------------------
# Faithful dt_util stub
# ---------------------------------------------------------------------------

class _FakeDtUtil:
    _tz: ZoneInfo = ZoneInfo("Europe/Tallinn")

    @classmethod
    def set_default_time_zone(cls, tz: ZoneInfo) -> None:
        cls._tz = tz

    @staticmethod
    def utc_from_timestamp(timestamp: float) -> datetime:
        return datetime.fromtimestamp(timestamp, tz=timezone.utc)

    @classmethod
    def as_local(cls, dt: datetime) -> datetime:
        return dt.astimezone(cls._tz)

    @staticmethod
    def now() -> datetime:
        return datetime.now(tz=timezone.utc)


# ---------------------------------------------------------------------------
# Minimal real stubs for class bases (MagicMock cannot be used as a base class)
# ---------------------------------------------------------------------------

class _CoordinatorEntityBase:
    def __init__(self, *args, **kwargs):
        pass


class _SensorEntityBase:
    def __init__(self, *args, **kwargs):
        pass


class _FakeSensorStateClass:
    MEASUREMENT = "measurement"


class _FakeDeviceInfo(dict):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)


# ---------------------------------------------------------------------------
# Build per-module mocks
# ---------------------------------------------------------------------------

_sensor_mod = MagicMock()
_sensor_mod.SensorEntity = _SensorEntityBase
_sensor_mod.SensorStateClass = _FakeSensorStateClass

_coordinator_mod = MagicMock()
_coordinator_mod.CoordinatorEntity = _CoordinatorEntityBase
_coordinator_mod.DataUpdateCoordinator = _CoordinatorEntityBase

_device_registry_mod = MagicMock()
_device_registry_mod.DeviceInfo = _FakeDeviceInfo

# `import homeassistant.util.dt as dt_util` resolves via attribute chain, not
# sys.modules key lookup, so we must wire .dt on the parent mock explicitly.
_ha_util_mock = MagicMock()
_ha_util_mock.dt = _FakeDtUtil

_ha_mock = MagicMock()
_ha_mock.util = _ha_util_mock

sys.modules.update(
    {
        "homeassistant": _ha_mock,
        "homeassistant.components": MagicMock(),
        "homeassistant.components.sensor": _sensor_mod,
        "homeassistant.config_entries": MagicMock(),
        "homeassistant.core": MagicMock(),
        "homeassistant.util": _ha_util_mock,
        "homeassistant.util.dt": _FakeDtUtil,
        "homeassistant.helpers": MagicMock(),
        "homeassistant.helpers.aiohttp_client": MagicMock(),
        "homeassistant.helpers.device_registry": _device_registry_mod,
        "homeassistant.helpers.entity_platform": MagicMock(),
        "homeassistant.helpers.event": MagicMock(),
        "homeassistant.helpers.update_coordinator": _coordinator_mod,
        "aiohttp": MagicMock(),
    }
)


# ---------------------------------------------------------------------------
# Timezone fixture
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def tallinn_tz():
    """Reset to Europe/Tallinn before each test, restore UTC after."""
    _FakeDtUtil._tz = ZoneInfo("Europe/Tallinn")
    yield
    _FakeDtUtil._tz = ZoneInfo("UTC")
