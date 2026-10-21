"""Publish the optional dashboard module; never edits dashboards or entities."""
from hashlib import sha256
import logging
from pathlib import Path
from urllib.parse import urlsplit

from homeassistant.components.frontend import add_extra_js_url
from homeassistant.components.http import StaticPathConfig
from homeassistant.components.lovelace.resources import ResourceStorageCollection
from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)
MODULE_PATH = Path(__file__).parent / "frontend" / "alphaess-dashboard.js"
MODULE_URL = "/alphaess_modbus/alphaess-dashboard.js"
CONTENT_PATH = MODULE_PATH.with_name("alphaess-dashboard-content.js")
CONTENT_URL = "/alphaess_modbus/alphaess-dashboard-content.js"


def _dashboard_digest() -> str:
    """Invalidate both modules when either changes."""
    return sha256(MODULE_PATH.read_bytes() + CONTENT_PATH.read_bytes()).hexdigest()[:16]


async def async_register_dashboard_resource(hass: HomeAssistant, url: str) -> None:
    """Provide a second loading path without replacing dashboards or YAML resources."""
    lovelace = hass.data.get("lovelace")
    # HA 2024 uses a dict; newer releases use LovelaceData.
    resources = (lovelace.get("resources") if isinstance(lovelace, dict)
                 else getattr(lovelace, "resources", None))
    if not isinstance(resources, ResourceStorageCollection):
        return
    await resources.async_get_info()  # Load persisted items before checking them.
    matches = []
    for item in resources.async_items():
        try:
            parts = urlsplit(item.get("url", ""))
        except ValueError:
            continue
        if not parts.scheme and not parts.netloc and parts.path == MODULE_URL:
            matches.append(item)
    if not matches:
        await resources.async_create_item({"url": url, "res_type": "module"})
        return
    # Preserve IDs; normalise existing copies to the same URL so browsers load
    # the module once. Never delete resources or touch other paths/origins.
    for item in matches:
        if item["url"] != url or item.get("type") != "module":
            await resources.async_update_item(
                item["id"], {"url": url, "res_type": "module"}
            )


async def async_setup_dashboard(hass: HomeAssistant) -> None:
    """Frontend is optional; its failure must not prevent inverter setup."""
    if "frontend" not in hass.config.components:
        return
    try:
        digest = await hass.async_add_executor_job(_dashboard_digest)
        await hass.http.async_register_static_paths([
            StaticPathConfig(MODULE_URL, str(MODULE_PATH), cache_headers=False),
            StaticPathConfig(CONTENT_URL, str(CONTENT_PATH), cache_headers=False),
        ])
        url = f"{MODULE_URL}?v={digest}"
        add_extra_js_url(hass, url)
    except Exception:
        _LOGGER.exception("Could not load optional AlphaESS dashboard module")
        return
    try:
        await async_register_dashboard_resource(hass, url)
    except Exception:
        _LOGGER.exception("Could not register optional AlphaESS dashboard resource")
