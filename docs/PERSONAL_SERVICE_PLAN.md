# Amplifier Personal — Early Adopter Plan

A self-contained personal AI service: an Amplifier instance that a non-technical user chats with via Slack, with file sharing through both Slack and cloud storage (OneDrive, Google Drive), running on a Linux server.

---

## The User Experience

The user has:
- A Slack workspace they own (free tier)
- An Amplifier instance they chat with (just type in a channel)
- A folder in OneDrive (or local filesystem) where files appear and sync
- Files shared back and forth through Slack and/or the synced folder

```
User's daily workflow:
  Slack:     "Hey, can you summarize the Q4 report I just uploaded?"
  Assistant: "Here's the summary. I've saved a clean version to your documents folder."
  OneDrive:  *Q4-report-summary.docx appears automatically*

  OneDrive:  *User drops meeting-notes.pdf into their folder*
  Slack:     "I noticed you added meeting notes. Want me to extract action items?"
```

---

## Architecture

```
User's devices (laptop, phone, tablet)
├── Slack (chat)                    ← Upload/download files, conversation
├── OneDrive / Google Drive         ← Drop files, see AI-created files
└── Web browser                     ← Admin panel (setup, status, config)

         │ Slack Socket Mode        │ rclone bisync         │ HTTP
         ▼                          ▼                       ▼

Linux Server
┌──────────────────────────────────────────────────────────────┐
│  amplifier-hive-slack (systemd service)                      │
│                                                              │
│  SlackConnector ←→ SessionManager ←→ Amplifier Session       │
│      │                     │                                 │
│      │ file upload     tool-filesystem / tool-bash           │
│      ▼                     ▼                                 │
│  Workspace Root: /srv/users/{name}/workspace/                │
│  ├── documents/     ← OneDrive sync (rclone) OR local path  │
│  ├── code/          ← Git sync OR local path (future)       │
│  ├── local/         ← Always local, never synced            │
│  │   └── .outbox/   ← Files to share back in Slack          │
│  └── .config/       ← Mount configuration                   │
│                                                              │
│  WorkspaceManager (rclone bisync on timer)                   │
│  AdminUI (NiceGUI on same FastAPI process, :8080/admin)      │
└──────────────────────────────────────────────────────────────┘
```

---

## Storage Backends

### The Key Principle: Filesystem IS the Interface

The Amplifier instance always works with local files via `tool-filesystem` and `tool-bash`. It never knows or cares HOW those files are synced. Storage backends are transparent — they just keep a local directory in sync with a remote source.

### Backend Types

| Backend | What It Does | When to Use |
|---------|-------------|-------------|
| **local** | Nothing — it's just a local directory. No sync. | Default. User's laptop with OneDrive app already syncing. Or server-only files. |
| **rclone** | Bidirectional sync via rclone (OneDrive, GDrive, S3, 70+ others) | Server running remotely from user. User accesses files via cloud storage. |
| **git** | Clone + auto-commit + push | Code projects, tools the instance builds, version-controlled content. |

### The `local` Backend (Simplest Case)

This is just a path on the filesystem. No sync, no daemon, no configuration beyond the path.

**Use case 1: User installs on their laptop**
- Their laptop already has OneDrive/GDrive desktop sync
- They point the workspace at a subfolder inside their synced folder
- Done. The existing OS-level sync handles everything.

```yaml
# mounts.yaml — laptop with OneDrive already syncing
mounts:
  documents:
    backend: local
    path: ~/OneDrive/AmplifierDocs
    
  projects:
    backend: local
    path: ~/Documents/Projects
```

**Use case 2: Some dirs synced, some not**
```yaml
mounts:
  documents:
    backend: rclone
    remote: "casey-onedrive:AmplifierDocs"
    sync_interval: 300
    
  scratch:
    backend: local
    path: ./scratch    # Just a local dir, relative to workspace root
    
  code/tools:
    backend: git
    repo: "https://github.com/casey/my-tools.git"
```

**Use case 3: Everything local (simplest possible)**
```yaml
# No mounts.yaml needed at all — entire workspace is just local files
# The working_dir in the main config IS the workspace
```

### The `rclone` Backend

For when the server is remote from the user and they need cloud storage sync.

**Setup:**
1. Install rclone on the server: `curl https://rclone.org/install.sh | sudo bash`
2. Configure a remote: `rclone config` (guided OAuth flow)
3. Add to mounts.yaml

**Sync behavior:**
- rclone bisync runs every N seconds (configurable, default 5 minutes)
- Bidirectional: local changes → cloud, cloud changes → local
- Conflict resolution: newest file wins, loser renamed to `.conflict`
- Managed by `WorkspaceManager` as a systemd timer

**rclone OAuth for headless servers:**
- Server prints a URL during `rclone config`
- User opens URL on any device with a browser
- Logs into Microsoft/Google, authorizes
- Token flows back to server automatically
- No browser needed on the server

### The `git` Backend (Future — Phase 4)

For code projects and version-controlled content.

**Behavior:**
- Clone on first sync
- Pull before the instance works on files
- Auto-commit after instance creates/modifies files
- Push on a configurable schedule or on-demand
- Instance can also use `tool-bash` to run git commands directly

---

## Mount Configuration Schema

```yaml
# .config/mounts.yaml

mounts:
  # Each key is a subdirectory name (relative to workspace root)
  
  <subdir-name>:
    # Required
    backend: local | rclone | git
    
    # For backend: local
    path: <absolute-or-relative-path>    # Default: ./<subdir-name>
    
    # For backend: rclone
    remote: "<rclone-remote>:<path>"     # e.g., "myonedrive:Documents/AI"
    sync_interval: 300                   # seconds between syncs (default: 300)
    conflict_resolve: newer              # newer | path1 | path2 (default: newer)
    exclude:                             # patterns to exclude from sync
      - "*.tmp"
      - ".DS_Store"
      - "Thumbs.db"
    
    # For backend: git
    repo: "<git-url>"
    branch: main                         # default: main
    auto_commit: true                    # commit after instance changes (default: true)
    auto_push: true                      # push after commit (default: true)
    commit_prefix: "[amplifier]"         # prefix for auto-commit messages
```

If no `mounts.yaml` exists, the entire workspace is treated as a single `local` backend — just a plain directory. This is the zero-configuration default.

---

## Slack File Handling

### Upload: User → Instance (Slack → Workspace)

When a user drags a file into a Slack channel:

1. Slack delivers a `message` event with `subtype: "file_share"` and a `files` array
2. Bot downloads each file from Slack's CDN (using bot token for auth)
3. Saves to workspace root (the instance can organize it later)
4. Tells the instance: `[User uploaded: report.pdf (1.2MB) → saved to ./report.pdf]`
5. Instance responds normally (can read, analyze, move the file)

**Implementation:** ~30 lines added to `slack.py`:
- Fix `_handle_message` guard to allow `subtype: "file_share"`
- Fix empty-text guard to allow file-only messages
- Add `_download_slack_file()` method

**Scopes needed:** `files:read`

### Download: Instance → User (Workspace → Slack)

The `.outbox/` convention:

1. Every prompt includes: `[To share files back, copy them to .outbox/]`
2. Instance uses `tool-bash` or `tool-filesystem` to copy files to `.outbox/`
3. After every `execute()`, connector checks `.outbox/` for files
4. Any files found are uploaded to the Slack thread and deleted from `.outbox/`

The instance already has the tools to do this — no Amplifier changes needed. The only new code is the outbox scanner in `slack.py` (~30 lines).

**Scopes needed:** `files:write`

### File Size Limits

| Constraint | Limit |
|------------|-------|
| Max file download | 50MB (configurable) |
| Slack free tier storage | 5GB total workspace |
| Max upload via API | 1GB per file |

Strategy: For large artifacts, save to workspace (syncs via OneDrive) rather than uploading to Slack. The instance should be coached to prefer cloud storage over Slack for anything > a few MB.

---

## User Onboarding Flow

### What the User Does (5 minutes)

**Step 1: Create a Slack workspace** (if they don't have one)
- Go to slack.com/create
- Follow the prompts (name, email, done)

**Step 2: Click the "Create App" link we provide**
- We generate a URL with our manifest pre-filled:
  ```
  https://api.slack.com/apps?new_app=1&manifest_yaml=<ENCODED_MANIFEST>
  ```
- User clicks it → Slack shows pre-filled app config → they click "Create"
- Then click "Install to Workspace" → approve scopes

**Step 3: Copy tokens to the admin UI (or send to us)**
- Bot token (`xoxb-...`)
- App-level token (`xapp-...`)
- Optionally: config token (for ongoing management)

**Step 4 (if remote server): Authorize OneDrive**
- Admin UI shows a link (from rclone OAuth)
- User clicks it, logs into Microsoft, authorizes
- Done — sync starts automatically

**Step 5: Set up their channel**
- Create a channel in Slack (e.g., `#assistant`)
- Set topic to `[instance:assistant]`
- Start chatting

### What the Service Does Automatically
- Creates the workspace directory structure
- Configures the Amplifier session with a personal assistant system prompt
- Starts the Slack connector
- Starts the rclone sync timer (if OneDrive configured)
- Creates the `.outbox/` directory

---

## Admin Web UI

Powered by NiceGUI (pure Python, mounts on same FastAPI process).

### Pages

**Dashboard** (`/admin`)
- Service status (running/stopped/failed)
- Slack connection status
- Storage sync status per mount
- Recent activity summary

**Slack Setup** (`/admin/slack`)
- "Create App" manifest link (one-click)
- Token input fields
- Connection test button
- Current scopes and events

**Storage** (`/admin/storage`)
- List of configured mounts with sync status
- Add/edit mounts
- OneDrive/GDrive OAuth button
- Manual sync trigger
- Last sync time, file counts, errors

**Logs** (`/admin/logs`)
- Live log viewer
- Filter by component (slack, storage, amplifier)

**Configuration** (`/admin/config`)
- Instance name and persona
- Working directory
- System prompt customization
- Provider API key management

---

## Implementation Phases

### Phase 1: File Upload/Download via Slack

**Scope:** ~90 lines in `slack.py` + manifest update
**Time:** 2-3 hours
**Independently valuable:** Yes — users can share files via Slack immediately

Changes:
- Fix subtype guard in `_handle_message` (allow `file_share`)
- Fix empty-text guard (allow file-only messages)
- Add `_download_slack_file()` — download from Slack CDN to working_dir
- Add `_process_outbox()` — scan `.outbox/`, upload to Slack thread
- Update `_build_prompt()` — add file descriptions + outbox instruction
- Add `files:read` + `files:write` to `slack-manifest.yaml`
- 12+ new tests

### Phase 2: Workspace Manager + rclone Sync

**Scope:** New `workspace_manager.py` module (~200 lines)
**Time:** 1 day
**Independently valuable:** Yes — enables cloud storage sync

Changes:
- `workspace_manager.py` — manages rclone bisync on a timer per mount
- Mount config schema (`mounts.yaml`)
- rclone helper (check installation, run sync, parse output)
- Systemd timer for periodic sync
- `hive-slack workspace sync` CLI command (manual trigger)
- `hive-slack workspace status` CLI command (show mount status)
- Support for `local`, `rclone` backends (git deferred to Phase 4)

### Phase 3: Admin Web UI

**Scope:** New `admin.py` module + NiceGUI dependency (~150 lines)
**Time:** 1-2 days

Changes:
- Add `nicegui` to dependencies
- `admin.py` — NiceGUI pages mounted on same process
- Dashboard, Slack setup, storage config, logs
- Manifest URL generator for new user onboarding
- rclone OAuth flow handler

### Phase 4: Git Backend + Multi-User + Polish

**Scope:** Extended workspace_manager + config improvements
**Time:** 1 week

Changes:
- Git backend in workspace_manager (clone, auto-commit, push)
- Multi-user support (multiple workspace configs on same server)
- Improved system prompt for personal assistant use case
- Onboarding wizard in admin UI
- Token rotation handling

---

## Deployment for the Early Adopter

### Prerequisites (on the server)

```bash
# rclone (for OneDrive sync)
curl https://rclone.org/install.sh | sudo bash

# The service
git clone https://github.com/bkrabach/amplifier-hive-slack.git
cd amplifier-hive-slack
uv venv .venv && source .venv/bin/activate
uv pip install -e .
```

### Configuration

```yaml
# config/casey.yaml
instances:
  assistant:
    bundle: foundation
    working_dir: /srv/users/casey/workspace
    persona:
      name: Casey's Assistant
      emoji: ":sparkles:"

defaults:
  instance: assistant

slack:
  app_token: ${SLACK_APP_TOKEN}
  bot_token: ${SLACK_BOT_TOKEN}
```

```yaml
# /srv/users/casey/workspace/.config/mounts.yaml
mounts:
  documents:
    backend: rclone
    remote: "casey-onedrive:AmplifierDocs"
    sync_interval: 300
```

### Launch

```bash
# Create workspace
mkdir -p /srv/users/casey/workspace

# Set up rclone OneDrive (one-time OAuth)
rclone config  # guided setup, user authorizes via URL

# Configure .env
cat > .env << 'EOF'
SLACK_APP_TOKEN=xapp-...
SLACK_BOT_TOKEN=xoxb-...
ANTHROPIC_API_KEY=sk-ant-...
EOF

# Install and start
hive-slack service install config/casey.yaml
hive-slack service start
```

### What the User Sees

```
#assistant channel (topic: [instance:assistant]):

Casey:      Hi! I just got set up.
Assistant:  Hey Casey! I'm your personal assistant. I can help with:
            • Documents and files (upload here or drop in your OneDrive folder)
            • Research and writing
            • Organizing projects and notes
            • Building tools and automations
            
            Your documents folder syncs with OneDrive — anything you
            drop there, I can see and work with. And anything I create
            will show up in your OneDrive automatically.
            
            What would you like to work on?

Casey:      [drags budget.xlsx into the chat]
Assistant:  Got it! I've saved budget.xlsx to your workspace.
            I can see it's a spreadsheet with Q1-Q4 data. Want me to:
            • Summarize the key numbers?
            • Create a chart or report?
            • Something else?
```

---

## Relationship to Main Quest (Milestones 3-6)

This sidequest runs in parallel with the main quest. The file handling (Phase 1) is the same code regardless — it goes into `slack.py` and benefits everyone.

| Sidequest Phase | Relationship to Main Quest |
|----------------|---------------------------|
| Phase 1 (files) | Directly useful for everyone — goes into the shared repo |
| Phase 2 (rclone) | New module, optional — only needed for remote server setups |
| Phase 3 (admin UI) | New module, optional — useful for any deployment |
| Phase 4 (git + multi-user) | Overlaps with Milestone 3 (instance-initiated actions) |

The `amplifier-hive-slack` repo stays as the single package. Storage management and admin UI are additive modules within it. When the Rust service (`amplifier-app-service`) is built, these modules get plucked out and generalized — same pattern as the `SessionManager` extraction.

---

## Open Questions

1. **Server choice for the early adopter** — spark-1 (current), spark-2, or a new box?
2. **OneDrive account type** — Personal or Business? (rclone config differs slightly)
3. **Persona customization** — Does the user want to name their assistant? Or just use a default?
4. **System prompt** — What's this user's primary use case? (general assistant? writing? project management? research?)
5. **Timeline** — How soon does the early adopter need to be up and running?
