# Admin Web UI — Complete Design

## Decision Summary

| Question | Decision | Rationale |
|----------|----------|-----------|
| Framework | NiceGUI | Pure Python, mounts on FastAPI, no build step, MIT |
| Process model | NiceGUI owns the event loop; bot is a startup task | NiceGUI docs recommend this; clean fallback when UI disabled |
| Port | 8080, configurable via `ADMIN_PORT` env var | High port, no root needed, standard for admin panels |
| Authentication | Single shared password, hashed with bcrypt | Simple for single-user; no user management overhead |
| Modify running bot? | View + restart only in MVP. No live config editing. | Editing live config is error-prone; restart is safe |
| Setup wizard | Stays as CLI. Admin UI links to it. | Setup runs before the bot exists — chicken-and-egg problem |

---

## Architecture

### Process Model: NiceGUI Owns the Loop

```
hive-slack config.yaml
    │
    ├─ admin_ui enabled? ──YES──► NiceGUI/uvicorn starts the event loop
    │                              ├── on_startup: service.start()
    │                              ├── on_startup: connector.start()
    │                              ├── Admin UI pages on /admin/*
    │                              └── on_shutdown: connector.stop(), service.stop()
    │
    └─ admin_ui disabled? ──NO──► Current asyncio.run(run()) path unchanged
```

**Why this works:** NiceGUI calls `ui.run()` which starts uvicorn, which starts
the asyncio event loop. The bot's `service.start()` and `connector.start()` are
just coroutines — they don't care who started the loop. We register them as
FastAPI startup events.

**The fallback:** When admin UI is disabled (no `nicegui` installed, or
`--no-admin` flag), `main.py` falls back to the existing `asyncio.run(run())`
path. Zero behavioral change for users who don't want the UI.

### Shared State Model

The admin UI reads state directly from the in-process objects. No IPC, no
database, no API layer between them — they share memory.

```python
# admin.py has direct references to these:
service: InProcessSessionManager   # sessions, bundles, config
connector: SlackConnector           # connection state, active executions
config: HiveSlackConfig             # static config
```

This is the key advantage of same-process: the admin UI can call
`len(service._sessions)` or check `connector._bot_user_id` directly. No
serialization, no polling overhead.

### File Layout

```
src/hive_slack/
├── main.py              # Modified: adds admin UI startup path
├── admin/               # NEW: admin UI package
│   ├── __init__.py      # init_admin(app, service, connector, config)
│   ├── auth.py          # Password auth middleware
│   ├── dashboard.py     # Dashboard page
│   ├── slack_setup.py   # Slack setup page
│   ├── configuration.py # Config viewer page
│   ├── logs.py          # Log viewer page
│   └── shared.py        # Shared layout (header, nav, theme)
├── config.py            # Modified: add optional admin section
└── ... (existing files unchanged)
```

### Changes to main.py

```python
async def run(config_path: str, *, admin: bool = True) -> None:
    """Load config, start service, connect to Slack, run."""
    # ... existing config loading and service.start() ...

    if admin and _nicegui_available():
        from hive_slack.admin import create_admin_app
        app = create_admin_app(service, connector, config)
        # NiceGUI owns the loop — bot starts via on_startup
        ui.run_with(
            app,
            port=int(os.environ.get("ADMIN_PORT", "8080")),
            title="Hive Slack Admin",
            favicon="🐝",
        )
    else:
        # Existing path — asyncio.run, stop_event.wait()
        await _run_headless(service, connector, config)


def _nicegui_available() -> bool:
    try:
        import nicegui  # noqa: F401
        return True
    except ImportError:
        return False
```

### Changes to config.py

Add an optional `admin` section to `HiveSlackConfig`:

```python
@dataclass
class AdminConfig:
    """Admin UI configuration. All fields optional."""
    enabled: bool = True
    port: int = 8080
    password_hash: str = ""  # bcrypt hash, empty = no auth
```

Config YAML (optional — admin works with zero config):

```yaml
admin:
  port: 8080
  password_hash: "$2b$12$..."  # from: python -c "import bcrypt; print(bcrypt.hashpw(b'mypass', bcrypt.gensalt()).decode())"
```

---

## Authentication

### Approach: Single Password with bcrypt

**Why not token-based or user accounts:** This is a single-user personal
service. The admin is one person. A username/password login with session cookies
is the simplest thing that works.

**Why not no-auth:** The service runs on a server, potentially exposed to the
network. Even behind SSH tunnels, defense in depth matters. Tokens in the admin
UI config could leak.

### Implementation

```python
# admin/auth.py

import bcrypt
from nicegui import ui, app
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import RedirectResponse

COOKIE_NAME = "hive_admin_session"
SESSION_MAX_AGE = 86400  # 24 hours

class AdminAuthMiddleware(BaseHTTPMiddleware):
    """Redirect unauthenticated requests to /admin/login."""

    def __init__(self, app, password_hash: str):
        super().__init__(app)
        self.password_hash = password_hash

    async def dispatch(self, request, call_next):
        # Skip auth for login page and static assets
        if request.url.path in ("/admin/login", "/admin/login/submit"):
            return await call_next(request)
        if not request.url.path.startswith("/admin"):
            return await call_next(request)

        # Check session cookie
        session_token = request.cookies.get(COOKIE_NAME)
        if not session_token or not _validate_session(session_token):
            return RedirectResponse("/admin/login")

        return await call_next(request)
```

**Login page:** A simple NiceGUI form with a single password field and a
"Sign In" button. On success, sets an HTTP-only session cookie.

**No auth mode:** If `password_hash` is empty in config, skip the middleware
entirely. For local-only deployments where the user doesn't care.

**Password setup:** A CLI helper generates the hash:
```bash
hive-slack admin set-password
# Enter password: ****
# Confirm: ****
# Add this to your config:
#   admin:
#     password_hash: "$2b$12$..."
```

### WebSocket Compatibility

NiceGUI uses WebSockets heavily. The auth middleware only gates the initial HTTP
request. Once the page loads and the WebSocket connects, NiceGUI's own session
management keeps it authenticated. This works fine through SSH tunnels — SSH
tunnels forward TCP, which includes both HTTP and WebSocket traffic.

---

## Page Designs

### Shared Layout (shared.py)

Every page gets the same shell:

```python
def admin_layout(title: str):
    """Shared page layout with header and navigation."""
    with ui.header().classes("bg-blue-900 text-white"):
        ui.label("Hive Slack Admin").classes("text-lg font-bold")
        with ui.row().classes("gap-4 ml-8"):
            ui.link("Dashboard", "/admin").classes("text-white")
            ui.link("Slack", "/admin/slack").classes("text-white")
            ui.link("Config", "/admin/config").classes("text-white")
            ui.link("Logs", "/admin/logs").classes("text-white")
        ui.space()
        # Live connection indicator in top-right
        status_dot()

def status_dot():
    """Green/red dot showing Slack connection status."""
    # Reads connector._bot_user_id — truthy means connected
    dot = ui.icon("circle").classes("text-xs")
    ui.timer(5.0, lambda: dot.classes(
        "text-green-500" if connector._bot_user_id else "text-red-500",
        replace="text-green-500 text-red-500"
    ))
```

**Theme:** Dark header (blue-900), white content area. NiceGUI default Quasar
theme. No custom CSS.

---

### Page 1: Dashboard (`/admin`)

**Priority: MUST-HAVE** — This is the reason the admin UI exists.

#### What It Shows

```
┌─────────────────────────────────────────────────────┐
│  Hive Slack Admin   Dashboard  Slack  Config  Logs  │  (header)
├─────────────────────────────────────────────────────┤
│                                                     │
│  ┌──────────┐  ┌──────────┐  ┌──────────┐          │
│  │ 🟢 Bot   │  │ 🟢 Slack │  │ 3        │          │
│  │ Running  │  │ Connected│  │ Sessions │          │
│  │ 2h 14m   │  │ Acme Inc │  │ active   │          │
│  └──────────┘  └──────────┘  └──────────┘          │
│                                                     │
│  Instances                                          │
│  ┌─────────────────────────────────────────────┐    │
│  │ ✨ Assistant  │ foundation │ 2 sessions     │    │
│  │ 🤖 Alpha     │ foundation │ 1 session      │    │
│  └─────────────────────────────────────────────┘    │
│                                                     │
│  Recent Errors (last 24h)                           │
│  ┌─────────────────────────────────────────────┐    │
│  │ 14:32  Slack API rate limited (retried OK)  │    │
│  │ 09:15  Bundle load timeout (recovered)      │    │
│  └─────────────────────────────────────────────┘    │
│  (empty state: "No errors in the last 24 hours")    │
│                                                     │
└─────────────────────────────────────────────────────┘
```

#### NiceGUI Components

```python
@ui.page("/admin")
def dashboard_page():
    admin_layout("Dashboard")

    with ui.row().classes("gap-4 w-full"):
        # Card 1: Bot status
        with ui.card().classes("w-64"):
            status_icon = ui.icon("circle")
            ui.label("Bot").classes("text-lg font-bold")
            status_label = ui.label("Starting...")
            uptime_label = ui.label("")

        # Card 2: Slack connection
        with ui.card().classes("w-64"):
            slack_icon = ui.icon("circle")
            ui.label("Slack").classes("text-lg font-bold")
            slack_label = ui.label("Connecting...")
            workspace_label = ui.label("")

        # Card 3: Sessions
        with ui.card().classes("w-64"):
            session_count = ui.label("0").classes("text-3xl font-bold")
            ui.label("Active Sessions")

    # Instances table
    ui.label("Instances").classes("text-lg font-bold mt-6")
    instances_table = ui.table(
        columns=[
            {"name": "persona", "label": "Instance", "field": "persona"},
            {"name": "bundle", "label": "Bundle", "field": "bundle"},
            {"name": "sessions", "label": "Sessions", "field": "sessions"},
            {"name": "working_dir", "label": "Working Dir", "field": "working_dir"},
        ],
        rows=[],
    ).classes("w-full")

    # Error log (recent)
    ui.label("Recent Errors").classes("text-lg font-bold mt-6")
    error_log = ui.log(max_lines=20).classes("w-full h-40")

    # Auto-refresh every 5 seconds
    def refresh():
        # Bot status
        is_running = bool(service._prepared)
        status_icon.classes(
            "text-green-500" if is_running else "text-red-500",
            replace="text-green-500 text-red-500"
        )
        status_label.text = "Running" if is_running else "Stopped"
        uptime_label.text = _format_uptime(start_time)

        # Slack status
        is_connected = bool(connector._bot_user_id)
        slack_icon.classes(
            "text-green-500" if is_connected else "text-red-500",
            replace="text-green-500 text-red-500"
        )
        slack_label.text = "Connected" if is_connected else "Disconnected"

        # Session count
        session_count.text = str(len(service._sessions))

        # Instances
        rows = []
        for name, inst in config.instances.items():
            count = sum(
                1 for k in service._sessions if k.startswith(f"{name}:")
            )
            rows.append({
                "persona": f"{inst.persona.emoji} {inst.persona.name}",
                "bundle": inst.bundle,
                "sessions": str(count),
                "working_dir": inst.working_dir,
            })
        instances_table.rows = rows

    ui.timer(5.0, refresh)
    refresh()  # immediate first render
```

#### How It Gets Data

| Data point | Source | Access pattern |
|-----------|--------|----------------|
| Bot running | `bool(service._prepared)` | Dict truthiness |
| Uptime | `time.time() - start_time` (capture at startup) | Simple subtraction |
| Slack connected | `bool(connector._bot_user_id)` | Set after `auth.test` in `connector.start()` |
| Workspace name | `connector._app.client.auth_test()` (cached) | One-time call, cache result |
| Session count | `len(service._sessions)` | Dict length |
| Per-instance sessions | Filter `service._sessions` keys by prefix | Key starts with `"{name}:"` |
| Recent errors | Custom log handler (see Logs section) | In-memory ring buffer |

---

### Page 2: Slack Setup (`/admin/slack`)

**Priority: MUST-HAVE** — This is the onboarding entry point.

#### What It Shows

Two modes based on whether Slack is already configured:

**Mode A: Not Yet Configured (no tokens in env)**
```
┌─────────────────────────────────────────────────────┐
│  Set Up Slack Connection                            │
│                                                     │
│  Step 1: Create your Slack app                      │
│  ┌─────────────────────────────────────────────┐    │
│  │  [Create Slack App]  (opens manifest URL)   │    │
│  │  This creates a pre-configured app with     │    │
│  │  all the right permissions.                 │    │
│  └─────────────────────────────────────────────┘    │
│                                                     │
│  Step 2: Enter your tokens                          │
│  ┌─────────────────────────────────────────────┐    │
│  │  Bot Token (xoxb-...):  [________________]  │    │
│  │  App Token (xapp-...):  [________________]  │    │
│  │                                             │    │
│  │  [Test Connection]  [Save]                  │    │
│  └─────────────────────────────────────────────┘    │
│                                                     │
└─────────────────────────────────────────────────────┘
```

**Mode B: Already Connected**
```
┌─────────────────────────────────────────────────────┐
│  Slack Connection                                   │
│                                                     │
│  Status: 🟢 Connected to "Acme Inc" workspace      │
│  Bot User: @amplifier (U0XXXXXX)                    │
│                                                     │
│  Scopes (12):                                       │
│  ┌─────────────────────────────────────────────┐    │
│  │ app_mentions:read  channels:history          │    │
│  │ channels:read      chat:write                │    │
│  │ chat:write.customize  files:read             │    │
│  │ files:write        groups:history            │    │
│  │ groups:read        im:history                │    │
│  │ im:read            reactions:read            │    │
│  │ reactions:write                              │    │
│  └─────────────────────────────────────────────┘    │
│                                                     │
│  Events (5):                                        │
│  ┌─────────────────────────────────────────────┐    │
│  │ app_mention  message.channels  message.groups│    │
│  │ message.im   reaction_added                  │    │
│  └─────────────────────────────────────────────┘    │
│                                                     │
│  [Test Connection]  [Reinstall URL]                 │
│                                                     │
└─────────────────────────────────────────────────────┘
```

#### NiceGUI Components

```python
@ui.page("/admin/slack")
def slack_setup_page():
    admin_layout("Slack Setup")

    is_connected = bool(connector._bot_user_id)

    if not is_connected and not config.slack.bot_token:
        # Mode A: Setup wizard
        _render_setup_mode()
    else:
        # Mode B: Status display
        _render_connected_mode()


def _render_setup_mode():
    ui.label("Set Up Slack Connection").classes("text-xl font-bold")

    # Step 1: Manifest link
    with ui.card().classes("w-full"):
        ui.label("Step 1: Create your Slack app").classes("font-bold")
        manifest_url = setup._generate_manifest_url()
        ui.link("Create Slack App", manifest_url, new_tab=True).classes(
            "bg-blue-600 text-white px-4 py-2 rounded"
        )
        ui.label("Opens Slack with all permissions pre-configured.")

    # Step 2: Token input
    with ui.card().classes("w-full mt-4"):
        ui.label("Step 2: Enter your tokens").classes("font-bold")
        bot_input = ui.input("Bot Token (xoxb-...)").classes("w-full")
        app_input = ui.input("App Token (xapp-...)").classes("w-full")

        with ui.row():
            ui.button("Test Connection", on_click=lambda: _test_tokens(
                bot_input.value, app_input.value, result_label
            ))
            ui.button("Save to .env", on_click=lambda: _save_tokens(
                bot_input.value, app_input.value, result_label
            ))
        result_label = ui.label("").classes("mt-2")


async def _test_tokens(bot_token: str, app_token: str, label):
    """Test Slack connection with provided tokens."""
    from slack_sdk.web.async_client import AsyncWebClient
    try:
        client = AsyncWebClient(token=bot_token)
        result = await client.auth_test()
        team = result.get("team", "unknown")
        user = result.get("user", "unknown")
        label.text = f"Connected to '{team}' as @{user}"
        label.classes("text-green-600", replace="text-red-600 text-green-600")
    except Exception as e:
        label.text = f"Connection failed: {e}"
        label.classes("text-red-600", replace="text-red-600 text-green-600")


def _render_connected_mode():
    ui.label("Slack Connection").classes("text-xl font-bold")

    with ui.card().classes("w-full"):
        ui.label(f"Connected as @{connector._bot_user_id}")
        # Scopes from the manifest in setup.py
        scopes = setup.SLACK_MANIFEST["oauth_config"]["scopes"]["bot"]
        ui.label(f"Scopes ({len(scopes)}):")
        with ui.row().classes("flex-wrap gap-1"):
            for scope in scopes:
                ui.badge(scope).classes("bg-blue-100 text-blue-800")

        events = setup.SLACK_MANIFEST["settings"]["event_subscriptions"]["bot_events"]
        ui.label(f"Events ({len(events)}):")
        with ui.row().classes("flex-wrap gap-1"):
            for event in events:
                ui.badge(event).classes("bg-green-100 text-green-800")

    with ui.row().classes("mt-4"):
        ui.button("Test Connection", on_click=lambda: _test_live_connection())
        ui.button("Copy Reinstall URL", on_click=lambda: _copy_reinstall_url())
```

#### How It Gets Data

| Data point | Source | Access |
|-----------|--------|--------|
| Is connected | `bool(connector._bot_user_id)` | Direct |
| Has tokens | `bool(config.slack.bot_token)` | Direct |
| Manifest URL | `setup._generate_manifest_url()` | Function call |
| Scopes list | `setup.SLACK_MANIFEST` dict | Static data |
| Events list | `setup.SLACK_MANIFEST` dict | Static data |
| Connection test | `slack_sdk.web.async_client.auth_test()` | Async API call |
| Reinstall URL | `slack_manifest.get_reinstall_url()` | API call (needs config token) |

#### Token Save Behavior

The "Save to .env" button appends/updates tokens in the `.env` file in the
working directory. It does NOT hot-reload them into the running process — the
user must restart the service. The UI shows a banner: "Tokens saved. Restart the
service to apply."

**Decision: No live token reload.** Restarting is safer and simpler than
hot-swapping Slack connections.

---

### Page 3: Configuration (`/admin/config`)

**Priority: MUST-HAVE** — Users need to see what's configured without reading YAML.

#### What It Shows

```
┌─────────────────────────────────────────────────────┐
│  Configuration (read-only)                          │
│                                                     │
│  Config file: /home/user/hive-slack/config/my.yaml  │
│                                                     │
│  Instances                                          │
│  ┌─────────────────────────────────────────────┐    │
│  │  ✨ Assistant                                │    │
│  │  Bundle: foundation                         │    │
│  │  Working Dir: /home/user/Documents/Amplifier│    │
│  │  Files: 12 files, 3 directories             │    │
│  └─────────────────────────────────────────────┘    │
│                                                     │
│  AI Provider                                        │
│  ┌─────────────────────────────────────────────┐    │
│  │  Provider: Anthropic (claude-sonnet-4-20250514)│    │
│  │  API Key: sk-ant-...****                    │    │
│  │  Status: ✅ Key detected from environment   │    │
│  └─────────────────────────────────────────────┘    │
│                                                     │
│  Working Directory Browser                          │
│  ┌─────────────────────────────────────────────┐    │
│  │  📁 documents/                              │    │
│  │  📁 .outbox/                                │    │
│  │  📄 notes.txt              2.1 KB           │    │
│  │  📄 report.pdf            145 KB            │    │
│  └─────────────────────────────────────────────┘    │
│                                                     │
│  Service                                            │
│  ┌─────────────────────────────────────────────┐    │
│  │  [Restart Service]                          │    │
│  └─────────────────────────────────────────────┘    │
│                                                     │
└─────────────────────────────────────────────────────┘
```

#### NiceGUI Components

```python
@ui.page("/admin/config")
def config_page():
    admin_layout("Configuration")

    ui.label("Configuration").classes("text-xl font-bold")
    ui.label("Read-only view of the running configuration.").classes(
        "text-gray-500"
    )

    # Instances
    for name, inst in config.instances.items():
        with ui.card().classes("w-full mt-4"):
            ui.label(f"{inst.persona.emoji} {inst.persona.name}").classes(
                "text-lg font-bold"
            )
            with ui.grid(columns=2).classes("gap-2"):
                ui.label("Bundle:"); ui.label(inst.bundle)
                ui.label("Working Dir:"); ui.label(inst.working_dir)

                # File count
                working_path = Path(inst.working_dir)
                if working_path.exists():
                    files = list(working_path.rglob("*"))
                    file_count = sum(1 for f in files if f.is_file())
                    dir_count = sum(1 for f in files if f.is_dir())
                    ui.label("Contents:")
                    ui.label(f"{file_count} files, {dir_count} directories")

    # Provider info
    with ui.card().classes("w-full mt-4"):
        ui.label("AI Provider").classes("text-lg font-bold")
        provider = service._detect_provider()
        if provider:
            ui.label(f"Module: {provider['module']}")
            model = provider["config"].get("model", "default")
            ui.label(f"Model: {model}")
            # Show masked key
            for env_var in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY",
                           "GOOGLE_API_KEY", "GEMINI_API_KEY"):
                key = os.environ.get(env_var, "")
                if key:
                    masked = key[:8] + "..." + key[-4:]
                    ui.label(f"{env_var}: {masked}")
        else:
            ui.label("No provider detected").classes("text-red-500")

    # Working directory browser (for default instance)
    default_inst = config.get_instance(config.default_instance)
    _render_file_browser(default_inst.working_dir)

    # Restart button
    with ui.card().classes("w-full mt-4"):
        ui.label("Service Control").classes("text-lg font-bold")
        restart_result = ui.label("")

        async def do_restart():
            restart_result.text = "Restarting..."
            try:
                from hive_slack import service_manager
                service_manager.restart()
                restart_result.text = "Restart signal sent. Page will reload."
                # The process is restarting — page will disconnect
            except Exception as e:
                restart_result.text = f"Restart failed: {e}"

        ui.button("Restart Service", on_click=do_restart).classes(
            "bg-orange-500 text-white"
        )
        ui.label(
            "Restarts the entire service. Active sessions will be lost."
        ).classes("text-sm text-gray-500")


def _render_file_browser(directory: str):
    """Simple file listing — not a full file manager."""
    ui.label("Working Directory").classes("text-lg font-bold mt-4")
    working_path = Path(directory)
    if not working_path.exists():
        ui.label(f"Directory does not exist: {directory}").classes("text-red-500")
        return

    rows = []
    for entry in sorted(working_path.iterdir()):
        if entry.name.startswith(".") and entry.name != ".outbox":
            continue
        icon = "folder" if entry.is_dir() else "description"
        size = ""
        if entry.is_file():
            size = _human_size(entry.stat().st_size)
        rows.append({
            "icon": icon,
            "name": entry.name + ("/" if entry.is_dir() else ""),
            "size": size,
        })

    ui.table(
        columns=[
            {"name": "name", "label": "Name", "field": "name"},
            {"name": "size", "label": "Size", "field": "size"},
        ],
        rows=rows,
    ).classes("w-full")
```

#### How It Gets Data

| Data point | Source | Access |
|-----------|--------|--------|
| Instance config | `config.instances` | Direct dict access |
| Provider info | `service._detect_provider()` | Static method |
| API key (masked) | `os.environ` | Env var lookup, mask in display |
| File listing | `Path(working_dir).iterdir()` | Filesystem |
| File counts | `Path(working_dir).rglob("*")` | Filesystem |

#### Why Read-Only

Editing config through a web UI means:
- Validating YAML structure
- Merging with the running config
- Handling conflicts between disk and memory
- Deciding when changes take effect (immediately? on restart?)

All of this is complexity we don't need for MVP. The user can edit the YAML file
directly (it's well-documented) and restart the service.

The ONE write action is "Restart Service" — because that's the thing the
non-technical user can't easily do from the CLI.

---

### Page 4: Logs (`/admin/logs`)

**Priority: MUST-HAVE** — "Is anything wrong?" is the #1 question.

#### What It Shows

```
┌─────────────────────────────────────────────────────┐
│  Logs                                               │
│                                                     │
│  [All] [Errors Only]    [Auto-scroll: ON]           │
│                                                     │
│  ┌─────────────────────────────────────────────┐    │
│  │ 14:32:01 hive_slack.slack INFO Connected    │    │
│  │ 14:32:01 hive_slack.service INFO Bundle OK  │    │
│  │ 14:32:05 hive_slack.slack INFO Message from │    │
│  │   user U0X in #general                      │    │
│  │ 14:32:06 hive_slack.service INFO Executing  │    │
│  │   for assistant in C0X:1234567              │    │
│  │ 14:33:12 hive_slack.slack INFO Response     │    │
│  │   posted (1247 chars)                       │    │
│  │ 14:35:00 hive_slack.slack WARNING Rate      │    │  <- yellow
│  │   limited, retrying in 5s                   │    │
│  │ 14:40:01 hive_slack.service ERROR Bundle    │    │  <- red
│  │   execution failed: TimeoutError            │    │
│  └─────────────────────────────────────────────┘    │
│                                                     │
└─────────────────────────────────────────────────────┘
```

#### Architecture: In-Memory Ring Buffer Log Handler

The key insight: Python's `logging` module supports multiple handlers. We add a
custom handler that captures log records into an in-memory ring buffer. The
admin UI reads from this buffer. No file parsing, no journalctl.

```python
# admin/logs.py

import logging
from collections import deque

class RingBufferHandler(logging.Handler):
    """Captures log records into a bounded deque for the admin UI."""

    def __init__(self, capacity: int = 2000):
        super().__init__()
        self.records: deque[logging.LogRecord] = deque(maxlen=capacity)

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


# Single global instance — attached during startup
log_buffer = RingBufferHandler(capacity=2000)


def install_log_handler():
    """Add the ring buffer handler to the root logger."""
    root = logging.getLogger()
    log_buffer.setLevel(logging.DEBUG)
    root.addHandler(log_buffer)
```

#### NiceGUI Components

```python
@ui.page("/admin/logs")
def logs_page():
    admin_layout("Logs")

    # Controls
    with ui.row().classes("items-center gap-4"):
        level_toggle = ui.toggle(
            ["All", "Warnings", "Errors"],
            value="All"
        )
        auto_scroll = ui.switch("Auto-scroll", value=True)

    # Log display
    log_display = ui.log(max_lines=500).classes(
        "w-full h-[calc(100vh-200px)] font-mono text-sm"
    )

    last_count = {"value": 0}

    def refresh():
        records = list(log_buffer.records)
        # Filter by level
        level_filter = level_toggle.value
        if level_filter == "Warnings":
            records = [r for r in records if r.levelno >= logging.WARNING]
        elif level_filter == "Errors":
            records = [r for r in records if r.levelno >= logging.ERROR]

        # Only push new records since last refresh
        new_records = records[last_count["value"]:]
        for record in new_records:
            line = log_buffer.format(record)
            log_display.push(line)

        last_count["value"] = len(records)

    # Poll every 1 second for new logs
    ui.timer(1.0, refresh)
    refresh()
```

#### Why Not WebSocket Streaming

NiceGUI's `ui.log` component already handles the display efficiently. Polling
every 1 second with a "push only new records" approach is simple, works through
SSH tunnels, and produces the same user experience as true streaming for a log
viewer. The ring buffer is bounded at 2000 records (~200KB of text max), so
memory is constant.

---

### Page 5: Storage (`/admin/storage`)

**Priority: NICE-TO-HAVE (defer to Phase 2)**

Storage/rclone is not implemented yet (per `PERSONAL_SERVICE_PLAN.md` Phase 2).
The admin UI for storage should be built alongside the `WorkspaceManager`
module, not before it. Designing UI for code that doesn't exist yet is waste.

**For MVP:** Show a placeholder page that says "Storage management coming soon"
and links to the rclone documentation.

```python
@ui.page("/admin/storage")
def storage_page():
    admin_layout("Storage")
    ui.label("Storage Management").classes("text-xl font-bold")
    ui.label("Storage backend management is not yet available.")
    ui.label("Currently, files are managed through:")
    with ui.column().classes("ml-4"):
        ui.label("- Upload files via Slack (drag and drop)")
        ui.label("- Download files from .outbox/ via Slack")
        ui.label("- Direct filesystem access in the working directory")
    ui.link(
        "Learn about planned storage backends",
        "https://github.com/bkrabach/amplifier-hive-slack/blob/main/docs/PERSONAL_SERVICE_PLAN.md#storage-backends",
        new_tab=True,
    )
```

**When to build the real page:** After `workspace_manager.py` exists and
provides an API like:
```python
workspace_manager.get_mounts() -> list[MountStatus]
workspace_manager.sync(mount_name) -> SyncResult
workspace_manager.get_last_sync(mount_name) -> datetime
```

---

### Page 6: Conversations (`/admin/conversations`)

**Priority: NICE-TO-HAVE (defer)**

#### Why Defer

The `InProcessSessionManager` stores sessions as opaque `AmplifierSession`
objects. Getting conversation history requires reaching into
`session.coordinator.get("context").get_messages()` — which is an async call
that touches Amplifier internals. The transcript is also saved to
`~/.amplifier/hive/sessions/` as JSONL, but parsing and rendering that
beautifully is UI work that doesn't help the core use case ("is my bot working?").

**For MVP:** Show session metadata only (what the dashboard already shows in its
instances table), linking to the transcript files on disk.

```python
@ui.page("/admin/conversations")
def conversations_page():
    admin_layout("Conversations")
    ui.label("Active Sessions").classes("text-xl font-bold")

    rows = []
    for key, session in service._sessions.items():
        instance, conv_id = key.split(":", 1)
        rows.append({
            "instance": instance,
            "conversation": conv_id,
            "status": "active" if key in service._locks else "idle",
        })

    ui.table(
        columns=[
            {"name": "instance", "label": "Instance", "field": "instance"},
            {"name": "conversation", "label": "Conversation", "field": "conversation"},
            {"name": "status", "label": "Status", "field": "status"},
        ],
        rows=rows,
    ).classes("w-full")

    ui.label("Transcripts are saved to:").classes("mt-4 text-sm text-gray-500")
    ui.label(str(SESSIONS_DIR)).classes("font-mono text-sm")
```

**When to build the real conversation viewer:** When there's a user request for
it. The transcripts exist as JSONL files — a motivated user can read them
directly. The admin UI's job is "is my bot working?", not "show me every
conversation."

---

## What NOT to Build

| Temptation | Why not | Alternative |
|-----------|---------|-------------|
| Live config editing | Complex validation, hot-reload is risky | Edit YAML + restart |
| User management / RBAC | Single-user product | One password |
| Conversation search | Amplifier internals, UI complexity | Grep the JSONL files |
| File upload through admin UI | Slack already does this | Use Slack |
| rclone OAuth through admin UI | rclone CLI handles this well | `rclone config` in terminal |
| Mobile-responsive design | Admin UI used on laptop/desktop | Desktop-only is fine |
| Custom CSS/theming | NiceGUI's defaults are adequate | Use default Quasar theme |
| API endpoints for external tools | No consumers exist | Add when needed |
| Metrics/charts/graphs | Overhead for a personal service | Logs tell the story |
| Multi-language / i18n | Single-user, English | English only |
| Dark mode toggle | Premature polish | Default light theme |

---

## Implementation Plan

### Phase 1: Skeleton + Dashboard (Day 1)

**Files to create:**
- `src/hive_slack/admin/__init__.py` (~40 lines) — `create_admin_app()`, FastAPI lifecycle
- `src/hive_slack/admin/shared.py` (~30 lines) — layout, nav, status dot
- `src/hive_slack/admin/dashboard.py` (~80 lines) — status cards, instances table, error log
- `src/hive_slack/admin/logs.py` (~60 lines) — `RingBufferHandler`, log page

**Files to modify:**
- `src/hive_slack/main.py` (~30 lines changed) — add admin startup path, `--no-admin` flag
- `pyproject.toml` (~3 lines) — add `nicegui` as optional dependency

**Total: ~240 new lines, ~30 modified lines**

Deliverable: Bot starts, dashboard shows green/red status, logs stream live.

### Phase 2: Slack Setup + Config Viewer (Day 2)

**Files to create:**
- `src/hive_slack/admin/slack_setup.py` (~100 lines) — manifest URL, token test, status
- `src/hive_slack/admin/configuration.py` (~90 lines) — config display, file browser, restart

**Files to modify:**
- `src/hive_slack/admin/__init__.py` (~5 lines) — register new pages

**Total: ~190 new lines, ~5 modified lines**

Deliverable: Full onboarding flow visible, config readable, restart button works.

### Phase 3: Authentication (Day 2-3)

**Files to create:**
- `src/hive_slack/admin/auth.py` (~80 lines) — middleware, login page, session cookie

**Files to modify:**
- `src/hive_slack/config.py` (~15 lines) — add `AdminConfig` dataclass
- `src/hive_slack/admin/__init__.py` (~10 lines) — mount auth middleware
- `src/hive_slack/main.py` (~5 lines) — add `admin set-password` CLI command

**Total: ~80 new lines, ~30 modified lines**

Deliverable: Password-protected admin UI.

### Phase 4: Placeholder Pages + Polish (Day 3)

**Files to create:**
- `src/hive_slack/admin/storage.py` (~20 lines) — placeholder
- `src/hive_slack/admin/conversations.py` (~40 lines) — session list

**Tests and polish:**
- `tests/test_admin.py` (~100 lines) — test page rendering, auth flow
- Manual testing through SSH tunnel

**Total: ~160 new lines**

### Effort Summary

| Phase | New Code | Modified | Calendar |
|-------|----------|----------|----------|
| 1: Skeleton + Dashboard | ~240 lines | ~30 lines | Day 1 |
| 2: Slack + Config | ~190 lines | ~5 lines | Day 2 |
| 3: Auth | ~80 lines | ~30 lines | Day 2-3 |
| 4: Placeholders + Tests | ~160 lines | 0 | Day 3 |
| **Total** | **~670 lines** | **~65 lines** | **~3 days** |

### Implementation Order

```
Day 1 morning:  pyproject.toml + main.py changes (get NiceGUI serving a blank page)
Day 1 afternoon: RingBufferHandler + Dashboard page + Logs page
Day 2 morning:  Slack Setup page
Day 2 afternoon: Config page + restart button + auth middleware
Day 3 morning:  Login page + password CLI + placeholder pages
Day 3 afternoon: Tests + manual testing + polish
```

### Dependency Addition

```toml
# pyproject.toml
[project.optional-dependencies]
admin = ["nicegui>=2.0", "bcrypt>=4.0"]
```

Install with: `pip install -e ".[admin]"` or `uv pip install -e ".[admin]"`

The admin UI is optional. Users who don't install the `admin` extra get the
existing headless behavior with no new dependencies.

---

## WebSocket / SSH Tunnel Compatibility

NiceGUI uses WebSockets for its real-time updates. WebSockets work through SSH
tunnels (`ssh -L 8080:localhost:8080 server`) because SSH tunnels forward raw
TCP. The browser connects to `localhost:8080` which tunnels to the server's
`localhost:8080`. Both HTTP and WebSocket traffic flow through the same TCP
connection.

The only thing that breaks: if a reverse proxy (nginx, cloudflare) sits in front
and doesn't forward WebSocket upgrades. For a personal service accessed via SSH
tunnel, this isn't an issue.

If the user exposes the port directly (no tunnel), NiceGUI works fine — it's
just a regular web server. The auth middleware protects it.

---

## How to Use This Design

This is the complete spec. The implementation order is:

1. Read this document
2. Follow Phase 1 → Phase 4 in order
3. Each phase is independently testable
4. Skip Phase 3 (auth) for initial development — add it before any non-localhost deployment

The `admin/` package is fully self-contained. It imports from the existing
`hive_slack` modules but nothing in the existing modules imports from `admin/`.
This means: if anything goes wrong, delete the `admin/` directory and you're
back to the original bot with zero side effects.
