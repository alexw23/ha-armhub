"""Sensors for UK Heat Network (ARM-Hub)."""
from __future__ import annotations

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfEnergy
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import ArmHubCoordinator


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: ArmHubCoordinator = hass.data[DOMAIN][config_entry.entry_id]
    async_add_entities(
        [
            ArmHubSensor(coordinator, property_id, desc)
            for property_id in coordinator.data
            for desc in SENSOR_DESCRIPTIONS
        ]
    )


# ── Sensor descriptors ────────────────────────────────────────────────────────

SENSOR_DESCRIPTIONS: list[dict] = [
    {
        "key":          "meter_reading",
        "name":         "Heat Usage",
        "unique_id":    "cumulative_kwh",
        "unit":         UnitOfEnergy.KILO_WATT_HOUR,
        "device_class": SensorDeviceClass.ENERGY,
        "state_class":  SensorStateClass.TOTAL_INCREASING,
    },
    {
        "key":          "energy_delta",
        "name":         "Energy Since Previous Reading",
        "unique_id":    "energy_delta",
        "unit":         UnitOfEnergy.KILO_WATT_HOUR,
        "device_class": SensorDeviceClass.ENERGY,
        "state_class":  SensorStateClass.TOTAL,
    },
    {
        "key":          "prev_meter_reading",
        "name":         "Previous Heat Usage",
        "unique_id":    "prev_meter_reading",
        "unit":         UnitOfEnergy.KILO_WATT_HOUR,
        "device_class": SensorDeviceClass.ENERGY,
        "state_class":  SensorStateClass.TOTAL_INCREASING,
    },
    {
        "key":          "reading_date",
        "name":         "Meter Read At",
        "unique_id":    "last_reading",
        "unit":         None,
        "device_class": SensorDeviceClass.TIMESTAMP,
        "state_class":  None,
    },
    {
        "key":          "prev_reading_date",
        "name":         "Previous Usage Read At",
        "unique_id":    "prev_reading_date",
        "unit":         None,
        "device_class": SensorDeviceClass.TIMESTAMP,
        "state_class":  None,
    },
    {
        "key":          "daily_kwh",
        "name":         "Daily Usage",
        "unique_id":    "daily_kwh",
        "unit":         UnitOfEnergy.KILO_WATT_HOUR,
        "device_class": SensorDeviceClass.ENERGY,
        "state_class":  SensorStateClass.TOTAL,
    },
    {
        "key":          "daily_cost",
        "name":         "Daily Cost",
        "unique_id":    "daily_cost",
        "unit":         "GBP",
        "device_class": SensorDeviceClass.MONETARY,
        "state_class":  SensorStateClass.TOTAL,
    },
    {
        "key":          "reading_age_h",
        "name":         "Reading Age",
        "unique_id":    "reading_age_h",
        "unit":         "h",
        "device_class": SensorDeviceClass.DURATION,
        "state_class":  SensorStateClass.MEASUREMENT,
    },
    {
        "key":          "unit_rate",
        "name":         "Unit Rate",
        "unique_id":    "unit_rate",
        "unit":         "GBP/kWh",
        "device_class": SensorDeviceClass.MONETARY,
        "state_class":  None,
    },
    {
        "key":          "standing_charge",
        "name":         "Standing Charge",
        "unique_id":    "standing_charge",
        "unit":         "GBP/d",
        "device_class": SensorDeviceClass.MONETARY,
        "state_class":  None,
    },
    {
        "key":          "meter_sn",
        "name":         "Meter Serial Number",
        "unique_id":    "meter_sn",
        "unit":         None,
        "device_class": None,
        "state_class":  None,
    },
    {
        "key":          "account_ref",
        "name":         "Account Reference",
        "unique_id":    "account_ref",
        "unit":         None,
        "device_class": None,
        "state_class":  None,
    },
    {
        "key":          "sync_time",
        "name":         "Last Sync",
        "unique_id":    "last_sync",
        "unit":         None,
        "device_class": SensorDeviceClass.TIMESTAMP,
        "state_class":  None,
    },
]


# ── Entity ────────────────────────────────────────────────────────────────────

class ArmHubSensor(CoordinatorEntity[ArmHubCoordinator], SensorEntity):
    """A sensor backed by coordinator data for one property."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: ArmHubCoordinator,
        property_id: str,
        desc: dict,
    ) -> None:
        super().__init__(coordinator)
        self._property_id    = property_id
        self._key            = desc["key"]
        self._attr_name      = desc["name"]
        self._attr_unique_id = f"{DOMAIN}_{property_id}_{desc['unique_id']}"
        if desc["unit"]:
            self._attr_native_unit_of_measurement = desc["unit"]
        if desc["device_class"]:
            self._attr_device_class = desc["device_class"]
        if desc["state_class"]:
            self._attr_state_class = desc["state_class"]

    @property
    def device_info(self) -> DeviceInfo | None:
        data = (self.coordinator.data or {}).get(self._property_id)
        if not data:
            return None
        account_ref = data.get("account_ref") or self._property_id
        return DeviceInfo(
            identifiers={(DOMAIN, self._property_id)},
            name=f"Heat Meter ({account_ref})",
            model=data.get("tariff_name"),
            manufacturer="ARM-Hub",
            serial_number=data.get("meter_sn"),
        )

    @property
    def native_value(self):
        data = (self.coordinator.data or {}).get(self._property_id)
        if not data:
            return None
        return data.get(self._key)
