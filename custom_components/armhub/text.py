"""MFA code text entity for UK Heat Network (ARM-Hub)."""
from __future__ import annotations

import re

from homeassistant.components.text import TextEntity, TextMode
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
    async_add_entities([MfaCodeText(coordinator)])


class MfaCodeText(CoordinatorEntity[ArmHubCoordinator], TextEntity):
    """Text entity into which the user (or an automation) writes the 6-digit MFA code."""

    _attr_has_entity_name   = True
    _attr_name              = "MFA Code"
    _attr_unique_id         = f"{DOMAIN}_mfa_code"
    _attr_mode              = TextMode.TEXT
    _attr_native_min        = 0
    _attr_native_max        = 6
    _attr_pattern           = r"\d{0,6}"
    _attr_native_value      = ""
    _attr_icon              = "mdi:shield-key"

    @property
    def device_info(self) -> DeviceInfo | None:
        return DeviceInfo(
            identifiers={(DOMAIN, self.coordinator.config_entry.entry_id)},
            name=f"ARM-Hub ({self.coordinator.hub_url})",
            manufacturer="ARM-Hub",
        )

    async def async_set_value(self, value: str) -> None:
        if not re.fullmatch(r"\d{6}", value):
            return
        await self.coordinator.submit_mfa_code(value)
        self._attr_native_value = ""
        self.async_write_ha_state()
