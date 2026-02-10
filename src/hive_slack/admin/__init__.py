"""Admin Web UI for Amplifier Hive Slack.

Optional NiceGUI-based admin panel that runs alongside the bot.
Import guard: if nicegui is not installed, this module is never loaded.
"""

from __future__ import annotations

import logging
import time


logger = logging.getLogger(__name__)

# Global references to bot components (set by create_admin_app)
_service = None
_connector = None
_config = None
_start_time: float = 0.0


def create_admin_app(service, connector, config) -> None:
    """Initialize the admin UI and register all pages.

    Called from main.py when admin UI is enabled. Sets up global
    references to the bot's service, connector, and config objects
    so all pages can read state directly.
    """
    global _service, _connector, _config, _start_time
    _service = service
    _connector = connector
    _config = config
    _start_time = time.time()

    # Set up authentication (login page + session storage)
    from hive_slack.admin.auth import is_auth_enabled, setup_login_page

    setup_login_page()
    if is_auth_enabled():
        logger.info("Admin UI authentication enabled")
    else:
        logger.info("Admin UI authentication disabled (no ADMIN_PASSWORD_HASH set)")

    # Import pages (registers routes)
    from hive_slack.admin import configuration  # noqa: F401
    from hive_slack.admin import dashboard  # noqa: F401
    from hive_slack.admin import logs  # noqa: F401
    from hive_slack.admin import slack_setup  # noqa: F401

    logger.info("Admin UI initialized with 4 pages")
