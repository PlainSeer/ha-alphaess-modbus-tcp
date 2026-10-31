"""Dashboard preferences, independent of frontend and inverter control."""
from .const import DOMAIN

CONF_MANAGE_DASHBOARD = "manage_dashboard"


def dashboard_management_enabled(hass):
    """Any opted-out entry pauses management of the shared dashboard."""
    entries = getattr(getattr(hass, "config_entries", None), "async_entries", None)
    return all(entry.options.get(CONF_MANAGE_DASHBOARD, True)
               for entry in entries(DOMAIN)) if entries else True
