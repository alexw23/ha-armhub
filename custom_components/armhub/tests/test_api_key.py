"""Tests for supplying the API key during setup."""
import pathlib
import re
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.config_entries import ConfigEntryState
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.armhub.config_flow import KEY_SNIPPET, ArmHubConfigFlow
from custom_components.armhub.const import API_HEADERS_COMMON, DOMAIN
from custom_components.armhub.coordinator import _auth_headers

FLOW_FIXTURES = pytest.mark.usefixtures("recorder_mock")

FAKE_KEY = "0123456789abcdef0123456789abcdef"
FORM = {
    "hub_url": "portal.example.com",
    "email": "user@example.com",
    "password": "secret",
    "api_key": FAKE_KEY,
    "addon_url": "http://addon:8099",
}


def test_no_key_in_the_source():
    root = pathlib.Path(__file__).parent.parent
    for path in root.rglob("*"):
        if path.is_file() and path.suffix in {".py", ".json", ".yaml"} and path.parent.name != "tests":
            assert not re.search(r"\b[0-9a-f]{32}\b", path.read_text()), path


def test_key_goes_in_the_headers():
    headers = _auth_headers("token", FAKE_KEY)
    assert headers["Ocp-Apim-Subscription-Key"] == FAKE_KEY
    assert headers["Authorization"] == "Bearer token"
    assert "Ocp-Apim-Subscription-Key" not in API_HEADERS_COMMON


def test_snippet_matches_the_form_of_the_portal_code():
    pattern = re.search(r"\.match\(/(.*)/\)", KEY_SNIPPET).group(1)
    minified = f'"Ocp-Apim-Subscription-Key":"".concat(null===M||void 0===c?void 0:"{FAKE_KEY}"),'
    assert re.search(pattern, minified).group(1) == FAKE_KEY


async def _start(hass):
    return await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})


@FLOW_FIXTURES
async def test_bad_key_is_refused(hass, enable_custom_integrations):
    result = await _start(hass)
    assert result["description_placeholders"]["key_snippet"] == KEY_SNIPPET
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {**FORM, "api_key": "not-a-key"}
    )
    assert result["errors"]["api_key"] == "invalid_api_key"


@FLOW_FIXTURES
async def test_key_is_saved_tidied_up(hass, enable_custom_integrations):
    token = {"access_token": "a", "refresh_token": "b", "expires_on": 1}
    with (
        patch.object(
            ArmHubConfigFlow,
            "_async_initiate_login",
            AsyncMock(return_value=("ok", None, token)),
        ),
        patch("custom_components.armhub.async_setup_entry", AsyncMock(return_value=True)),
    ):
        result = await _start(hass)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {**FORM, "api_key": f"  {FAKE_KEY.upper()} "}
        )
        await hass.async_block_till_done()
    # The login finishes in the background, which completes the flow by itself.
    entry = hass.config_entries.async_entries(DOMAIN)[0]
    assert entry.data["api_key"] == FAKE_KEY


@FLOW_FIXTURES
async def test_entry_without_a_key_asks_for_reconfigure(hass, enable_custom_integrations):
    entry = MockConfigEntry(domain=DOMAIN, data={k: v for k, v in FORM.items() if k != "api_key"})
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.SETUP_ERROR


@FLOW_FIXTURES
async def test_reconfigure_saves_the_key(hass, enable_custom_integrations):
    entry = MockConfigEntry(domain=DOMAIN, data={k: v for k, v in FORM.items() if k != "api_key"})
    entry.add_to_hass(hass)
    with patch("custom_components.armhub.async_setup_entry", AsyncMock(return_value=True)):
        result = await entry.start_reconfigure_flow(hass)
        result = await hass.config_entries.flow.async_configure(result["flow_id"], FORM)
        await hass.async_block_till_done()
    assert result["reason"] == "reconfigure_successful"
    assert entry.data["api_key"] == FAKE_KEY
