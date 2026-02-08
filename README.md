# Amplifier Hive Slack

Run multiple [Amplifier](https://github.com/microsoft/amplifier) AI instances as teammates in a Slack workspace. Each instance has its own persona (name + avatar), its own working directory, and its own conversation history. Just type in a channel — no @mentions needed.

## What It Does

```
#with-alpha channel:
  You:    What files are in this project?
  Alpha:  Here's what I found in the directory...

#with-beta channel:
  You:    Review the auth module
  Beta:   Looking at the code, I notice several issues...
```

- **Channel-per-instance** — Set a channel topic like `[instance:alpha]` and just type. No @mention, no slash commands.
- **Multiple instances** — Each with distinct name, avatar, bundle, and working directory.
- **Natural addressing** — In shared channels, say `beta: review this` or `@beta what do you think?`
- **Thread continuity** — Follow-up messages in a thread use the same session.
- **DMs** — DM the bot directly for private conversations.
- **Reactions** — React with :repeat: to regenerate a response.
- **Full Amplifier power** — File system, bash, web search, code intelligence, agent delegation — all the tools.

## Quickstart (5 minutes)

### Prerequisites

- Python 3.10+
- An LLM provider API key (Anthropic or OpenAI)
- A Slack workspace you control

### 1. Install

```bash
# Clone the repo
git clone https://github.com/bkrabach/amplifier-hive-slack.git
cd amplifier-hive-slack

# Create venv and install
uv venv .venv && source .venv/bin/activate
uv pip install -e ".[dev]"
```

### 2. Create a Slack App

1. Go to [api.slack.com/apps](https://api.slack.com/apps) and click **Create New App** > **From scratch**
2. Name it anything (e.g., "Amplifier"), select your workspace
3. **Socket Mode** > Enable > Generate App-Level Token with `connections:write` scope. Save the `xapp-...` token.
4. **OAuth & Permissions** > Bot Token Scopes, add all of these:
   ```
   app_mentions:read    channels:history    channels:read
   chat:write           chat:write.customize
   groups:history       groups:read
   im:history           im:read
   reactions:read       reactions:write
   ```
5. **Event Subscriptions** > Subscribe to bot events:
   ```
   app_mention    message.channels    message.groups
   message.im     reaction_added
   ```
6. **Install to Workspace** > Copy the `xoxb-...` Bot Token

### 3. Configure

Create a `.env` file (never committed to git):

```bash
cat > .env << 'EOF'
SLACK_APP_TOKEN=xapp-your-app-token
SLACK_BOT_TOKEN=xoxb-your-bot-token
ANTHROPIC_API_KEY=sk-ant-your-key
EOF
```

Edit `config/example.yaml` to set your working directory:

```yaml
instances:
  alpha:
    bundle: foundation
    working_dir: ~/my-project    # Where this instance can read/write files
    persona:
      name: Alpha
      emoji: ":robot_face:"

defaults:
  instance: alpha

slack:
  app_token: ${SLACK_APP_TOKEN}
  bot_token: ${SLACK_BOT_TOKEN}
```

### 4. Run

```bash
source .venv/bin/activate
set -a; source .env; set +a
python -m hive_slack.main config/example.yaml
```

You should see:
```
Instance 'alpha' ready (Alpha :robot_face:, bundle=foundation)
Connecting to Slack with instances: Alpha :robot_face:
⚡️ Bolt app is running!
```

### 5. Set Up Channels

In Slack:
1. Create a channel (e.g., `#with-alpha`)
2. Set the channel topic to: `[instance:alpha]`
3. Invite the bot: `/invite @YourAppName`
4. Type anything — Alpha responds. No @mention needed.

## Run as a Service

Install as a systemd service for persistent background operation:

```bash
# Install (writes systemd unit file, enables auto-start)
python -m hive_slack.main service install config/example.yaml

# Start
python -m hive_slack.main service start

# Check status
python -m hive_slack.main service status
# 🟢 running: Running (PID 12345)

# View logs
python -m hive_slack.main service logs
python -m hive_slack.main service logs -f  # follow mode

# Restart (after config or code changes)
python -m hive_slack.main service restart

# Stop
python -m hive_slack.main service stop

# Uninstall
python -m hive_slack.main service uninstall
```

The service auto-restarts on crash and runs even when you're logged out.

## Multiple Instances

Add more instances in `config/example.yaml`:

```yaml
instances:
  alpha:
    bundle: foundation
    working_dir: ~/project-a
    persona:
      name: Alpha
      emoji: ":robot_face:"

  beta:
    bundle: foundation
    working_dir: ~/project-b
    persona:
      name: Beta
      emoji: ":gear:"

defaults:
  instance: alpha
```

In shared channels, address a specific instance naturally:

```
beta: review this code
@beta what do you think?
hey beta, look at this
```

Messages without an instance name go to the channel's default.

## Channel Configuration

Control channel behavior through Slack channel topics (editable in the UI):

| Topic Directive | Behavior |
|-----------------|----------|
| `[instance:alpha]` | All messages go to Alpha. Just type. |
| `[default:alpha]` | Alpha by default, `beta: ...` to override. |
| `[mode:roundtable]` | All instances see messages (coming soon). |
| *(no directive)* | @mention the bot required. |

Mix directives with regular topic text: `Coding help [instance:alpha]`

## Slack App Management

Manage the Slack app configuration programmatically:

```bash
# View current app config
python -m hive_slack.main slack status

# Export manifest to file
python -m hive_slack.main slack export config/slack-manifest.yaml

# Push local manifest changes to Slack
python -m hive_slack.main slack sync config/slack-manifest.yaml
```

Requires a Slack configuration token. Generate one at [api.slack.com/apps](https://api.slack.com/apps) > Your App Configuration Tokens, then add to `.env`:

```bash
SLACK_APP_ID=your-app-id
SLACK_CONFIG_TOKEN=xoxe.xoxp-your-config-token
SLACK_CONFIG_REFRESH_TOKEN=xoxe-your-refresh-token
```

## Architecture

```
Slack Workspace
  #with-alpha  #with-beta  #general  DMs
       |            |          |       |
       +----+-------+----------+------+
            |
    SlackConnector (Socket Mode)
      - Channel topic routing
      - Natural addressing
      - Persona management
      - Markdown formatting
            |
    SessionManager.execute(instance, conversation, prompt) -> response
            |
    InProcessSessionManager
      - Bundle loading (one per unique bundle)
      - Per-conversation sessions with locking
      - Transcript persistence (JSONL)
            |
    Amplifier Core + Foundation
      - Session lifecycle
      - LLM providers (Anthropic, OpenAI)
      - Tools (filesystem, bash, web, search, etc.)
```

The `SessionManager` interface is the key architectural boundary. Today it runs in-process. In the future, it will be replaced by a gRPC client talking to a Rust service — with zero changes to the Slack connector code.

## Configuration Reference

### Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `SLACK_APP_TOKEN` | Yes | Socket Mode app-level token (`xapp-...`) |
| `SLACK_BOT_TOKEN` | Yes | Bot user OAuth token (`xoxb-...`) |
| `ANTHROPIC_API_KEY` | Yes* | Anthropic API key (auto-detected) |
| `OPENAI_API_KEY` | Yes* | OpenAI API key (fallback if no Anthropic key) |
| `LOG_LEVEL` | No | Set to `DEBUG` for verbose output (default: `INFO`) |
| `SLACK_APP_ID` | No | For Slack manifest management |
| `SLACK_CONFIG_TOKEN` | No | For Slack manifest management |
| `SLACK_CONFIG_REFRESH_TOKEN` | No | For Slack manifest management |

*At least one LLM provider key is required.

### Config File (`config/example.yaml`)

```yaml
instances:
  <name>:
    bundle: <bundle-name-or-path>    # Default: foundation
    working_dir: <path>              # Where this instance operates (~ expanded)
    persona:
      name: <display-name>          # Shown in Slack messages
      emoji: <slack-emoji>           # e.g., ":robot_face:"

defaults:
  instance: <name>                   # Default instance when none specified

slack:
  app_token: ${SLACK_APP_TOKEN}
  bot_token: ${SLACK_BOT_TOKEN}
```

## Slack Free Tier

This project works entirely within the Slack free plan. Key constraints:

- **10 apps max** — We use 1 app with `chat:write.customize` for unlimited personas
- **90-day message history** — Sessions persist independently in JSONL
- **5 GB file storage** — Store large artifacts externally, share links
- **Unlimited channels** and **unlimited members**

See [docs/SLACK_FREE_TIER.md](docs/SLACK_FREE_TIER.md) for the complete reference.

## Development

```bash
# Install with dev dependencies
uv pip install -e ".[dev]"

# Run tests (82 tests)
pytest tests/ -v

# Run local service test (no Slack needed)
python -m hive_slack.test_harness local

# Check service status and logs
python -m hive_slack.test_harness status
```

## Multi-Machine Setup

Run the bot on multiple machines pointed at the same Slack workspace:

1. Install on each machine
2. Configure different instances per machine in the config file
3. Each machine connects via Socket Mode (Slack supports up to 10 connections per app)
4. Messages are routed to the correct instance regardless of which machine it runs on

## License

MIT
