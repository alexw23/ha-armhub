"""MFA pending binary sensor for UK Heat Network (ARM-Hub)."""
from __future__ import annotations

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.config_entries import ConfigEntry
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
    async_add_entities([MfaPendingBinarySensor(coordinator)])


class MfaPendingBinarySensor(CoordinatorEntity[ArmHubCoordinator], BinarySensorEntity):
    """ON when the auth addon is waiting for an MFA code."""

    _attr_has_entity_name = True
    _attr_name            = "MFA Pending"
    _attr_unique_id       = f"{DOMAIN}_mfa_pending"
    _attr_device_class    = BinarySensorDeviceClass.RUNNING
    _attr_icon            = "mdi:message-lock"

    @property
    def device_info(self) -> DeviceInfo | None:
        return DeviceInfo(
            identifiers={(DOMAIN, self.coordinator.config_entry.entry_id)},
            name=f"ARM-Hub ({self.coordinator.hub_url})",
            manufacturer="ARM-Hub",
        )

    @property
    def is_on(self) -> bool:
        return self.coordinator.mfa_pending
