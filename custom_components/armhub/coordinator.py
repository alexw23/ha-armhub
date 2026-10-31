"""Coordinator for UK Heat Network (ARM-Hub)."""
from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone

import aiohttp

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.models import StatisticData, StatisticMeanType, StatisticMetaData
from homeassistant.components.recorder.statistics import (
    async_add_external_statistics,
    get_last_statistics,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD, UnitOfEnergy
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util.unit_conversion import EnergyConverter
from homeassistant.util import dt as dt_util

from .const import (
    API_HEADERS_COMMON,
    ARMADILLO_DETAIL_URL,
    ARMADILLO_PROPERTY_DATA_URL,
    CONF_ADDON_URL,
    CONF_API_KEY,
    CONF_HUB_URL,
    CONF_IMAP_ENTITY,
    DISCOVERY_INTERVAL_HOURS,
    DOMAIN,
    POLL_INTERVAL_MINUTES,
)

_LOGGER = logging.getLogger(__name__)


def _auth_headers(access_token: str, api_key: str) -> dict:
    return {
        "Authorization": f"Bearer {access_token}",
        "Ocp-Apim-Subscription-Key": api_key,
        **API_HEADERS_COMMON,
    }


class ArmHubCoordinator(DataUpdateCoordinator[dict]):
    """
    One coordinator per config entry (= one ARM-Hub account).

    coordinator.data shape:
    {
        "<property_id>": {
            "property_id":    "PRO_xxxx",
            "tariff_id":      "TAI_xxxx",
            "account_ref":    "...",
            "tariff_name":    "...",
            "meter_sn":       "...",
            "meter_reading":  123.4,
            "reading_date":   "2026-...",
            "reading_ts":     1234567890,
            "unit_rate":      0.08,
            "standing_charge":0.30,
            "sync_time":      "...",
            "reading_interval": 0.25,
            "daily_kwh":      1.23,
            "daily_cost":     0.15,
        },
        ...
    }
    """

    def __init__(self, hass: HomeAssistant, config_entry: ConfigEntry) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=config_entry,
            name=DOMAIN,
            update_interval=timedelta(minutes=POLL_INTERVAL_MINUTES),
        )
        self._hub_url: str    = config_entry.data.get(CONF_HUB_URL, "arm-hub.com")
        self._addon_url: str  = config_entry.data[CONF_ADDON_URL]
        self._api_key: str    = config_entry.data[CONF_API_KEY]
        self._email:        str       = config_entry.data[CONF_EMAIL]
        self._password:     str       = config_entry.data[CONF_PASSWORD]
        self._imap_entity:  str | None = config_entry.data.get(CONF_IMAP_ENTITY)

        self._mfa_pending: bool = False

        # {property_id: tariff_instance_id}
        self._devices: dict[str, str] = {}
        self._last_discovery: datetime | None = None

        self._store: Store = Store(hass, 1, f"{DOMAIN}.{config_entry.entry_id}")
        # {property_id: {"prev_reading_ts": ..., "prev_meter_reading": ...}}
        self._stored: dict = {}


    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def async_config_entry_first_refresh(self) -> None:
        self._stored = await self._store.async_load() or {}
        await super().async_config_entry_first_refresh()

    # ── Public API ────────────────────────────────────────────────────────────

    @property
    def hub_url(self) -> str:
        return self._hub_url

    @property
    def mfa_pending(self) -> bool:
        return self._mfa_pending

    @property
    def devices(self) -> dict[str, str]:
        """Return {property_id: tariff_id} map."""
        return self._devices

    async def submit_mfa_code(self, code: str) -> None:
        session = async_get_clientsession(self.hass)
        async with session.post(
            f"{self._addon_url}/mfa",
            json={"code": code},
            timeout=aiohttp.ClientTimeout(total=10),
        ) as resp:
            resp.raise_for_status()
        _LOGGER.info("MFA code submitted to addon")

    # ── Core update ───────────────────────────────────────────────────────────

    async def _async_update_data(self) -> dict:
        token = await self._async_get_token()
        await self._maybe_discover(token)

        results: dict = {}
        errors: list[str] = []
        for property_id, tariff_id in self._devices.items():
            try:
                details = await self._async_fetch_details(token, property_id, tariff_id)
                await self._async_insert_statistics(property_id, details)
                results[property_id] = details
            except Exception as exc:  # noqa: BLE001
                _LOGGER.warning("Error fetching property %s: %s", property_id, exc, exc_info=True)
                errors.append(str(exc))

        if not results and errors:
            raise UpdateFailed("; ".join(errors))

        return results

    # ── Discovery ─────────────────────────────────────────────────────────────

    async def _maybe_discover(self, token: str) -> None:
        """Discover/refresh properties if no devices or every 24 h."""
        now = datetime.now(tz=timezone.utc)
        if (
            self._devices
            and self._last_discovery is not None
            and (now - self._last_discovery) < timedelta(hours=DISCOVERY_INTERVAL_HOURS)
        ):
            return
        await self._async_discover_devices(token)
        self._last_discovery = now

    async def _async_discover_devices(self, token: str) -> None:
        """Fetch /propertyData and build {property_id: tariff_instance_id} map."""
        session = async_get_clientsession(self.hass)
        async with session.get(
            ARMADILLO_PROPERTY_DATA_URL,
            headers=_auth_headers(token, self._api_key),
            timeout=aiohttp.ClientTimeout(total=30),
        ) as resp:
            resp.raise_for_status()
            body = await resp.json(content_type=None)

        if body.get("status") != "Success" or not body.get("data"):
            raise UpdateFailed(f"Property discovery failed: {body}")

        devices: dict[str, str] = {}
        for prop in body["data"]:
            property_id = prop.get("propertyID", "")
            for utility in prop.get("utilities", []):
                tariff_id = utility.get("liveTariffInstanceID", "")
                if property_id and tariff_id:
                    devices[property_id] = tariff_id
                    _LOGGER.debug("Discovered property %s → tariff %s", property_id, tariff_id)

        if not devices:
            raise UpdateFailed("No Heating utilities found on this account")

        self._devices = devices
        _LOGGER.info("Discovered %d device(s) for account %s", len(devices), self._email)

    # ── Token acquisition ─────────────────────────────────────────────────────

    async def _async_get_token(self) -> str:
        """POST credentials to the auth addon and return the access token.
        The addon owns all token lifecycle — we just authenticate each call."""
        session = async_get_clientsession(self.hass)
        try:
            async with session.post(
                f"{self._addon_url}/token",
                json={
                    "email":       self._email,
                    "password":    self._password,
                    "imap_entity": self._imap_entity or "",
                },
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status == 401:
                    raise UpdateFailed("Auth addon rejected credentials — check email/password in integration config")
                data = await resp.json(content_type=None)
        except UpdateFailed:
            raise
        except Exception as exc:
            raise UpdateFailed(f"Cannot reach auth addon at {self._addon_url}: {exc}") from exc

        if data.get("access_token"):
            self._set_mfa_pending(False)
            return data["access_token"]

        status = data.get("status", "")
        if status == "mfa_pending":
            self._set_mfa_pending(True)
            raise UpdateFailed(
                "MFA required — enter your code in the ARM-Hub Auth add-on panel, "
                "then reload the integration"
            )
        if status in ("logging_in", "refreshing"):
            raise UpdateFailed(f"Token being acquired ({status}) — will retry")
        raise UpdateFailed(f"Auth addon error: {data.get('message', status)}")

    def _set_mfa_pending(self, pending: bool) -> None:
        if self._mfa_pending == pending:
            return
        self._mfa_pending = pending
        self.async_update_listeners()

    # ── Per-device Armadillo fetch ────────────────────────────────────────────

    async def _async_fetch_details(self, token: str, property_id: str, tariff_id: str) -> dict:
        session = async_get_clientsession(self.hass)
        async with session.get(
            ARMADILLO_DETAIL_URL,
            params={
                "tariffInstanceID":          tariff_id,
                "incTariffInstanceExtended": "true",
                "incRateDetails":            "true",
            },
            headers=_auth_headers(token, self._api_key),
            timeout=aiohttp.ClientTimeout(total=30),
        ) as resp:
            if not resp.ok:
                raise UpdateFailed(f"HTTP {resp.status} from Armadillo API for {property_id}")
            body = await resp.json(content_type=None)

        if body.get("status") != "Success" or not body.get("data"):
            raise UpdateFailed(f"Tariff detail fetch failed for {property_id}: {body}")

        entry = body["data"][0]
        ti    = entry["tariffInstance"]
        ext   = entry["tariffInstanceExtended"]

        meter_reading = float(ext["meterReading"])
        reading_ts    = int(ext["meterReadingTimeStamp"])
        reading_dt    = datetime.fromtimestamp(reading_ts, tz=timezone.utc)

        prev_data    = self._stored.setdefault(property_id, {})
        prev_ts      = prev_data.get("prev_reading_ts")
        prev_reading = prev_data.get("prev_meter_reading")
        prev_reading_dt = datetime.fromtimestamp(int(prev_ts), tz=timezone.utc) if prev_ts else None

        interval_h   : float | None = None
        energy_delta : float | None = None

        if prev_ts and prev_reading is not None and reading_ts != int(prev_ts):
            interval_h   = round((reading_ts - int(prev_ts)) / 3600, 4)
            energy_delta = round(meter_reading - float(prev_reading), 4)
        else:
            energy_delta = prev_data.get("last_energy_delta")
            interval_h   = prev_data.get("last_interval_h")

        unit_rate      = float(ext.get("perUnitChargeValue", 0))
        standing_chg   = float(ext.get("fixedRateChargeValue", 0))

        # Daily tracking — reset accumulator at midnight (local time)
        now_local         = datetime.now()
        today_str         = now_local.strftime("%Y-%m-%d")
        day_start_date    = prev_data.get("day_start_date")
        day_start_reading = prev_data.get("day_start_reading")

        if day_start_date != today_str:
            prev_data["day_start_date"]    = today_str
            prev_data["day_start_reading"] = meter_reading
            day_start_reading = meter_reading

        daily_kwh: float = round(meter_reading - float(day_start_reading), 4)
        elapsed_h: float = now_local.hour + now_local.minute / 60 + now_local.second / 3600
        daily_cost: float = round(daily_kwh * unit_rate + standing_chg * elapsed_h / 24, 6)

        return {
            "property_id":        property_id,
            "tariff_id":          tariff_id,
            "account_ref":        ti.get("accountRef", "unknown"),
            "tariff_name":        ti.get("tariffName", ""),
            "meter_sn":           ext.get("meterSN", ""),
            "meter_reading":      meter_reading,
            "reading_ts":         reading_ts,
            "reading_date":       reading_dt,
            "unit_rate":          unit_rate,
            "standing_charge":    standing_chg,
            "sync_time":          datetime.now(tz=timezone.utc),
            "reading_interval":   interval_h,
            "reading_age_h":      interval_h,
            "energy_delta":       energy_delta,
            "daily_kwh":          daily_kwh,
            "daily_cost":         daily_cost,
            "prev_meter_reading": float(prev_reading) if prev_reading is not None else None,
            "prev_reading_date":  prev_reading_dt,
        }

    # ── Statistics insertion ──────────────────────────────────────────────────

    async def _async_insert_statistics(self, property_id: str, details: dict) -> None:
        reading_ts    = details["reading_ts"]
        meter_reading = details["meter_reading"]
        account_ref   = details["account_ref"]

        safe_ref     = re.sub(r"[^a-z0-9_]", "_", account_ref.lower())
        statistic_id = f"{DOMAIN}:hiu_{safe_ref}_consumption"

        metadata = StatisticMetaData(
            mean_type=StatisticMeanType.NONE,
            has_sum=True,
            name=f"HIU {account_ref} energy consumption",
            source=DOMAIN,
            statistic_id=statistic_id,
            unit_class=EnergyConverter.UNIT_CLASS,
            unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        )

        prev_data    = self._stored.setdefault(property_id, {})
        prev_ts      = prev_data.get("prev_reading_ts")
        prev_reading = prev_data.get("prev_meter_reading")

        _LOGGER.debug(
            "STAT CHECK %s: reading_ts=%s prev_ts=%s meter=%.3f prev=%.3f",
            statistic_id, reading_ts, prev_ts, meter_reading,
            float(prev_reading) if prev_reading is not None else -1,
        )
        _LOGGER.debug(
            "STAT TS DEBUG %s: local=%s utc=%s",
            statistic_id,
            datetime.fromtimestamp(reading_ts),
            datetime.fromtimestamp(reading_ts, tz=timezone.utc),
        )

        if prev_ts and reading_ts == int(prev_ts):
            _LOGGER.debug("STAT SKIP %s: reading_ts unchanged (%s)", statistic_id, reading_ts)
            await self._store.async_save(self._stored)  # persist day_start changes
            return

        if prev_reading is None:
            _LOGGER.debug("STAT BOOTSTRAP %s: storing first reading ts=%s meter=%.3f", statistic_id, reading_ts, meter_reading)
            prev_data["prev_reading_ts"]    = reading_ts
            prev_data["prev_meter_reading"] = meter_reading
            await self._store.async_save(self._stored)
            return

        delta = meter_reading - float(prev_reading)
        if delta < 0:
            _LOGGER.warning("Negative delta %.3f kWh for %s — skipping", delta, property_id)
            prev_data["prev_reading_ts"]    = reading_ts
            prev_data["prev_meter_reading"] = meter_reading
            await self._store.async_save(self._stored)
            return
        if delta == 0:
            _LOGGER.debug("Zero delta for %s — meter read but no consumption, skipping stat", property_id)
            prev_data["prev_reading_ts"] = reading_ts
            await self._store.async_save(self._stored)
            return

        last_stat = await get_instance(self.hass).async_add_executor_job(
            get_last_statistics, self.hass, 1, statistic_id, True, {"sum", "state"}
        )
        last_sum = float(last_stat[statistic_id][0].get("sum") or 0) if last_stat else 0.0
        if last_stat:
            last_entry = last_stat[statistic_id][0]
            _LOGGER.debug(
                "STAT LAST %s: start=%s sum=%.3f state=%s",
                statistic_id,
                last_entry.get("start"),
                last_sum,
                last_entry.get("state"),
            )
        else:
            _LOGGER.debug("STAT LAST %s: no existing statistics found", statistic_id)

        new_sum  = last_sum + delta
        reading_utc = dt_util.utc_from_timestamp(reading_ts)
        if reading_utc.minute == 0 and reading_utc.second == 0:
            reading_dt = reading_utc.replace(microsecond=0) - timedelta(hours=1)
        else:
            reading_dt = reading_utc.replace(minute=0, second=0, microsecond=0)

        _LOGGER.debug(
            "STAT INSERT %s: Δ%.3f → Σ%.3f @ %s (raw_ts=%s)",
            statistic_id, delta, new_sum, reading_dt, reading_ts,
        )
        async_add_external_statistics(self.hass, metadata, [StatisticData(start=reading_dt, state=delta, sum=new_sum)])

        # ── Cost statistic ────────────────────────────────────────────────────
        unit_rate    = details["unit_rate"]
        standing_chg = details["standing_charge"]
        interval_h   = round((reading_ts - int(prev_ts)) / 3600, 4) if prev_ts else None
        if unit_rate and interval_h:
            energy_cost   = delta * unit_rate
            standing_cost = standing_chg * interval_h / 24
            cost_delta    = round(energy_cost + standing_cost, 6)

            cost_stat_id = f"{DOMAIN}:hiu_{safe_ref}_cost"
            last_cost_stat = await get_instance(self.hass).async_add_executor_job(
                get_last_statistics, self.hass, 1, cost_stat_id, True, {"sum"}
            )
            last_cost_sum = float(last_cost_stat[cost_stat_id][0].get("sum") or 0) if last_cost_stat else 0.0
            new_cost_sum  = round(last_cost_sum + cost_delta, 6)

            cost_metadata = StatisticMetaData(
                mean_type=StatisticMeanType.NONE,
                has_sum=True,
                name=f"HIU {account_ref} cost",
                source=DOMAIN,
                statistic_id=cost_stat_id,
                unit_class=None,
                unit_of_measurement="GBP",
            )
            async_add_external_statistics(
                self.hass, cost_metadata,
                [StatisticData(start=reading_dt, state=cost_delta, sum=new_cost_sum)],
            )
            _LOGGER.debug("COST INSERT %s: Δ£%.4f → Σ£%.4f", cost_stat_id, cost_delta, new_cost_sum)

        interval_h = round((reading_ts - int(prev_ts)) / 3600, 4) if prev_ts else None
        prev_data["last_energy_delta"] = delta
        prev_data["last_interval_h"]   = interval_h
        prev_data["prev_reading_ts"]    = reading_ts
        prev_data["prev_meter_reading"] = meter_reading
        await self._store.async_save(self._stored)
