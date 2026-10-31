"""Constants for the UK Heat Network (ARM-Hub) integration."""

DOMAIN = "armhub"

CONF_HUB_URL     = "hub_url"
CONF_PROPERTY_ID = "property_id"
CONF_ADDON_URL   = "addon_url"
CONF_IMAP_ENTITY = "imap_entity"
CONF_API_KEY     = "api_key"

DEFAULT_HUB_URL   = "spot-on-energy.arm-hub.com"
DEFAULT_ADDON_URL = "http://60356650-armhubmeter:8099"

POLL_INTERVAL_MINUTES    = 5
DISCOVERY_INTERVAL_HOURS = 24

ARMADILLO_BASE_URL          = "https://api.armadillo-analytics.com"
ARMADILLO_TARIFFS_URL       = f"{ARMADILLO_BASE_URL}/bil/tariffs/tariffInstances"
ARMADILLO_DETAIL_URL        = f"{ARMADILLO_BASE_URL}/bil/tariffs/tariffInstance"
ARMADILLO_PROPERTY_DATA_URL = f"{ARMADILLO_BASE_URL}/bil/reports/propertyData"

API_HEADERS_COMMON = {
    "Content-Type":              "application/json",
    "User-Agent":                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Origin":                    "https://spot-on-energy.arm-hub.com",
    "Referer":                   "https://spot-on-energy.arm-hub.com/",
}
