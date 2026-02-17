"""Simple password authentication for the admin UI.

Uses a SHA-256 hashed password stored in ADMIN_PASSWORD_HASH env var.
If no hash is set, auth is disabled (open access for local-only use).

Session is tracked via NiceGUI's app.storage.user mechanism —
a secure cookie-based session store built into NiceGUI.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os

from nicegui import app, ui

logger = logging.getLogger(__name__)

SESSION_KEY = "admin_authenticated"


def get_password_hash() -> str:
    """Get the configured password hash, or empty string for no-auth mode."""
    return os.environ.get("ADMIN_PASSWORD_HASH", "")


def is_auth_enabled() -> bool:
    """Check if authentication is configured."""
    return bool(get_password_hash())


def is_authenticated() -> bool:
    """Check if the current user session is authenticated."""
    if not is_auth_enabled():
        return True  # No auth configured — always authenticated
    return app.storage.user.get(SESSION_KEY, False)


def verify_password(password: str) -> bool:
    """Verify a password against the stored hash."""
    expected = get_password_hash()
    if not expected:
        return True
    actual = hashlib.sha256(password.encode()).hexdigest()
    return hmac.compare_digest(actual, expected)


def require_auth() -> bool:
    """Check auth and redirect to login if needed. Returns True if authenticated."""
    if is_authenticated():
        return True
    ui.navigate.to("/admin/login")
    return False


def setup_login_page() -> None:
    """Register the login page route."""

    @ui.page("/admin/login")
    def login_page() -> None:
        """Render the login page."""
        if is_authenticated():
            ui.navigate.to("/admin")
            return

        with ui.column().classes("absolute-center items-center gap-4"):
            ui.label("🐝 Hive Slack Admin").classes("text-2xl font-bold")
            ui.label("Enter the admin password to continue.").classes("text-gray-500")

            password_input = (
                ui.input("Password", password=True, password_toggle_button=True)
                .classes("w-64")
                .on("keydown.enter", lambda: do_login())
            )

            error_label = ui.label("").classes("text-red-600")

            def do_login():
                if verify_password(password_input.value):
                    app.storage.user[SESSION_KEY] = True
                    ui.navigate.to("/admin")
                else:
                    error_label.text = "Incorrect password."
                    password_input.value = ""

            ui.button("Sign In", on_click=do_login).classes("w-64")
