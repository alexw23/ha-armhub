"""Config flow for UK Heat Network (ARM-Hub)."""
from __future__ import annotations

import asyncio
import logging
import re
from urllib.parse import urlparse

_LOGGER = logging.getLogger(__name__)

import aiohttp
import voluptuous as vol

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import CONF_ADDON_URL, CONF_API_KEY, CONF_HUB_URL, CONF_IMAP_ENTITY, DEFAULT_ADDON_URL, DEFAULT_HUB_URL, DOMAIN

_MFA_CODE = "mfa_code"

# Shown in the setup form. Run in the browser console on the heat network's portal,
# it prints the key the portal's own pages use. Passed as a placeholder because
# Home Assistant would read the braces in a translation string as placeholders.
KEY_SNIPPET = (
    "(async()=>{for(const s of document.querySelectorAll("
    "'script[src*=\"_next/static/chunks\"]')){const m=(await(await fetch(s.src)).text())"
    ".match(/Ocp-Apim-Subscription-Key[^,]*?\"([0-9a-f]{32})\"/);"
    "if(m)return console.log(m[1])}console.log('Key not found')})()"
)


def _normalise_hub_url(raw: str) -> str:
    """Extract bare hostname from any URL-like input.

    Accepts 'spoton.arm-hub.com', 'https://spoton.arm-hub.com/dashboard', etc.
    Returns 'spoton.arm-hub.com' or empty string on failure.
    """
    raw = raw.strip()
    if not raw:
        return ""
    if "://" not in raw:
        raw = "https://" + raw
    return urlparse(raw).netloc.lower()


def _normalise_api_key(raw: str) -> str:
    """Return the key in the form the API expects, or empty string if malformed."""
    key = raw.strip().lower()
    return key if re.fullmatch(r"[0-9a-f]{32}", key) else ""


def _detect_imap_entity(hass) -> str:
    """Return the first likely IMAP sensor entity_id, or empty string."""
    for state in hass.states.async_all("sensor"):
        eid = state.entity_id
        if re.search(r"imap", eid, re.IGNORECASE):
            return eid
    return ""


class ArmHubConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle the config flow."""

    VERSION = 1

    def __init__(self) -> None:
        self._hub_url: str = ""
        self._email: str = ""
        self._password: str = ""
        self._addon_url: str = DEFAULT_ADDON_URL
        self._api_key: str = ""
        self._imap_entity: str | None = None
        self._login_error: str | None = None
        self._token_data: dict | None = None
        self._login_task: asyncio.Task | None = None

    # ── Step 1: credentials ───────────────────────────────────────────────────

    async def async_step_user(
        self, user_input: dict | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        imap_default = _detect_imap_entity(self.hass)

        # Carry forward any error surfaced by the background login task
        if self._login_error:
            errors[CONF_ADDON_URL] = self._login_error
            self._login_error = None

        if user_input is not None:
            hub_url     = _normalise_hub_url(user_input[CONF_HUB_URL])
            email       = user_input[CONF_EMAIL].strip()
            password    = user_input[CONF_PASSWORD]
            addon_url   = user_input[CONF_ADDON_URL].rstrip("/")
            api_key     = _normalise_api_key(user_input[CONF_API_KEY])
            imap_entity = user_input.get(CONF_IMAP_ENTITY, "").strip() or None

            if not hub_url:
                errors[CONF_HUB_URL] = "invalid_url"
            if not email:
                errors[CONF_EMAIL] = "required"
            if not api_key:
                errors[CONF_API_KEY] = "invalid_api_key"

            if not errors:
                await self.async_set_unique_id(f"armhub_{hub_url}_{email.lower()}")
                self._abort_if_unique_id_configured()

                self._hub_url     = hub_url
                self._email       = email
                self._password    = password
                self._addon_url   = addon_url
                self._api_key     = api_key
                self._imap_entity = imap_entity

                return await self.async_step_login_progress()

        step_schema = vol.Schema(
            {
                vol.Required(CONF_HUB_URL,    default=self._hub_url or DEFAULT_HUB_URL): str,
                vol.Required(CONF_EMAIL,      default=self._email or ""): str,
                vol.Required(CONF_PASSWORD):   str,
                vol.Required(CONF_API_KEY,    default=self._api_key or ""): str,
                vol.Required(CONF_ADDON_URL,  default=self._addon_url or DEFAULT_ADDON_URL): str,
                vol.Optional(CONF_IMAP_ENTITY, default=self._imap_entity or imap_default or ""): str,
            }
        )
        return self.async_show_form(
            step_id="user",
            data_schema=step_schema,
            errors=errors,
            description_placeholders={"key_snippet": KEY_SNIPPET},
        )

    # ── Step 1b: spinner while the addon initiates login ─────────────────────

    async def async_step_login_progress(
        self, user_input: dict | None = None
    ) -> ConfigFlowResult:
        """Show a progress spinner while the addon logs in and wait for a status."""
        if self._login_task is None:
            _LOGGER.debug(
                "Starting login task for %s via %s", self._email, self._addon_url
            )
            self._login_task = self.hass.async_create_task(
                self._async_initiate_login()
            )

        if not self._login_task.done():
            return self.async_show_progress(
                step_id="login_progress",
                progress_action="logging_in",
                progress_task=self._login_task,
            )

        try:
            status, error_key, token_data = self._login_task.result()
        except Exception as exc:  # noqa: BLE001
            _LOGGER.exception("Login task raised unexpectedly: %s", exc)
            self._login_task = None
            self._login_error = "addon_error"
            return self.async_show_progress_done(next_step_id="user")

        self._login_task = None
        _LOGGER.debug("Login task finished: status=%s error=%s", status, error_key)

        if error_key:
            # Surface the error on the credentials form
            self._login_error = error_key
            return self.async_show_progress_done(next_step_id="user")

        if status == "ok" and token_data:
            # Login succeeded without MFA
            self._token_data = token_data
            return self.async_show_progress_done(next_step_id="finish")

        # MFA code required — show the code entry form
        return self.async_show_progress_done(next_step_id="mfa")

    # ── Step 1c: entry creation after no-MFA login ───────────────────────────

    async def async_step_finish(
        self, user_input: dict | None = None
    ) -> ConfigFlowResult:
        return self._create_entry(self._token_data)

    # ── Step 2: MFA code entry ────────────────────────────────────────────────

    async def async_step_mfa(
        self, user_input: dict | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}

        if user_input is not None:
            code = user_input.get(_MFA_CODE, "").strip()
            if not re.fullmatch(r"\d{6}", code):
                errors[_MFA_CODE] = "invalid_mfa_code"
            else:
                token_data, error_key = await self._async_submit_mfa(code)
                if error_key:
                    errors[_MFA_CODE] = error_key
                elif token_data:
                    return self._create_entry(token_data)

        imap_note = (
            "IMAP integration detected — future codes will be submitted automatically."
            if self._imap_entity
            else "No IMAP integration detected. After setup, enter future codes via the MFA Code entity, or configure IMAP email forwarding."
        )
        return self.async_show_form(
            step_id="mfa",
            data_schema=vol.Schema({vol.Required(_MFA_CODE): str}),
            description_placeholders={"imap_note": imap_note},
            errors=errors,
        )

    # ── Reconfigure (no MFA needed — coordinator handles it) ─────────────────

    async def async_step_reconfigure(
        self, user_input: dict | None = None
    ) -> ConfigFlowResult:
        entry = self._get_reconfigure_entry()
        errors: dict[str, str] = {}
        imap_default = entry.data.get(CONF_IMAP_ENTITY) or _detect_imap_entity(self.hass)

        step_schema = vol.Schema(
            {
                vol.Required(CONF_HUB_URL,    default=entry.data.get(CONF_HUB_URL, DEFAULT_HUB_URL)): str,
                vol.Required(CONF_EMAIL,      default=entry.data.get(CONF_EMAIL, "")): str,
                vol.Required(CONF_PASSWORD,   default=entry.data.get(CONF_PASSWORD, "")): str,
                vol.Required(CONF_API_KEY,    default=entry.data.get(CONF_API_KEY, "")): str,
                vol.Required(CONF_ADDON_URL,  default=entry.data.get(CONF_ADDON_URL, DEFAULT_ADDON_URL)): str,
                vol.Optional(CONF_IMAP_ENTITY, default=imap_default or ""): str,
            }
        )

        if user_input is not None:
            hub_url     = _normalise_hub_url(user_input[CONF_HUB_URL])
            email       = user_input[CONF_EMAIL].strip()
            password    = user_input[CONF_PASSWORD]
            addon_url   = user_input[CONF_ADDON_URL].rstrip("/")
            api_key     = _normalise_api_key(user_input[CONF_API_KEY])
            imap_entity = user_input.get(CONF_IMAP_ENTITY, "").strip() or None

            if not hub_url:
                errors[CONF_HUB_URL] = "invalid_url"
            if not email:
                errors[CONF_EMAIL] = "required"
            if not api_key:
                errors[CONF_API_KEY] = "invalid_api_key"

            if not errors:
                return self.async_update_reload_and_abort(
                    entry,
                    data={
                        CONF_HUB_URL:     hub_url,
                        CONF_EMAIL:       email,
                        CONF_PASSWORD:    password,
                        CONF_ADDON_URL:   addon_url,
                        CONF_API_KEY:     api_key,
                        CONF_IMAP_ENTITY: imap_entity,
                    },
                )

        return self.async_show_form(
            step_id="reconfigure",
            data_schema=step_schema,
            errors=errors,
            description_placeholders={"key_snippet": KEY_SNIPPET},
        )

    # ── Helpers ───────────────────────────────────────────────────────────────

    async def _async_initiate_login(self) -> tuple[str, str | None, dict | None]:
        """POST credentials to addon and poll until status is known.

        Returns (status, error_key, token_data).
        status is 'ok', 'mfa_pending', or 'error'.
        """
        url = f"{self._addon_url}/token"
        _LOGGER.debug("POSTing credentials to %s", url)
        session = async_get_clientsession(self.hass)
        try:
            for attempt in range(6):  # up to ~30 s
                async with session.post(
                    url,
                    json={"email": self._email, "password": self._password, "imap_entity": self._imap_entity or ""},
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    data = await resp.json(content_type=None)

                status = data.get("status", "ok")
                _LOGGER.debug("Addon /token attempt %d: status=%s", attempt + 1, status)
                if status == "ok":
                    return "ok", None, data
                if status == "mfa_pending":
                    return "mfa_pending", None, None
                if status in ("logging_in", "refreshing"):
                    await asyncio.sleep(5)
                    continue
                return "error", "addon_error", None

            # Timed out — MFA must be on the way
            return "mfa_pending", None, None

        except aiohttp.ClientConnectorError as exc:
            _LOGGER.warning("Cannot reach addon at %s: %s", self._addon_url, exc)
            return "error", "addon_unreachable", None
        except Exception as exc:
            _LOGGER.exception("Unexpected error during addon login: %s", exc)
            return "error", "addon_error", None

    async def _async_submit_mfa(self, code: str) -> tuple[dict | None, str | None]:
        """Submit a 6-digit MFA code and poll until the addon returns a token.

        Returns (token_data, error_key).
        """
        session = async_get_clientsession(self.hass)
        try:
            async with session.post(
                f"{self._addon_url}/mfa",
                json={"code": code},
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                resp.raise_for_status()
        except Exception:
            return None, "mfa_submit_failed"

        try:
            for attempt in range(30):  # up to ~150 s — B2C post-MFA flow can be slow
                await asyncio.sleep(5)
                async with session.post(
                    f"{self._addon_url}/token",
                    json={"email": self._email, "password": self._password},
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    data = await resp.json(content_type=None)

                status = data.get("status", "ok")
                _LOGGER.debug("MFA poll attempt %d: status=%s", attempt + 1, status)
                if status == "ok":
                    return data, None
                if status in ("mfa_pending", "logging_in"):
                    continue
                if status == "error":
                    _LOGGER.error("Addon login failed after MFA: %s", data.get("message"))
                    return None, "addon_login_failed"
                return None, "mfa_submit_failed"

            return None, "mfa_timeout"
        except Exception:
            return None, "mfa_submit_failed"

    def _create_entry(self, bootstrap_token: dict | None = None) -> ConfigFlowResult:
        data: dict = {
            CONF_HUB_URL:     self._hub_url,
            CONF_EMAIL:       self._email,
            CONF_PASSWORD:    self._password,
            CONF_ADDON_URL:   self._addon_url,
            CONF_API_KEY:     self._api_key,
            CONF_IMAP_ENTITY: self._imap_entity,
        }
        if bootstrap_token:
            # Coordinator consumes this on first run to avoid triggering another login
            data["bootstrap_token"] = {
                "access_token":  bootstrap_token.get("access_token"),
                "refresh_token": bootstrap_token.get("refresh_token"),
                "expires_on":    bootstrap_token.get("expires_on"),
            }
        return self.async_create_entry(title=f"ARM-Hub ({self._hub_url})", data=data)
