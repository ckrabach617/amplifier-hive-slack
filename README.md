# Amplifier Hive Slack

An AI assistant that lives in your Slack workspace. Powered by [Amplifier](https://github.com/microsoft/amplifier), with full access to files, code, web search, and more. Just type — no commands, no @mentions needed.

## What It Does

```
#assistant channel:
  You:        Can you summarize the Q4 report I just uploaded?
  Amplifier:  Here's the summary. I've saved a clean version
              to your documents folder.

  You:        [drags budget.xlsx into chat]
  Amplifier:  Got it! I can see it's a spreadsheet with Q1-Q4
              data. Want me to create a chart?
```

- **Just type** — Set up a channel and start talking. No @mentions, no slash commands.
- **File sharing** — Drag files into Slack, get files back. Works with OneDrive, Google Drive, or any synced folder.
- **Thread = conversation** — Each thread remembers everything. New message in the channel = fresh start.
- **Mid-execution steering** — Send follow-up messages while it's working. They get incorporated in real-time.
- **Progress indicators** — See what it's doing (⏳ received, ⚙️ working, 📨 queued).
- **Full Amplifier power** — File system, bash, web search, code intelligence, agent delegation.

## Setup (5 minutes)

### Prerequisites

- Python 3.10+ (and [uv](https://docs.astral.sh/uv/))
- An AI provider API key (Anthropic, OpenAI, or Google Gemini)
- A Slack workspace you control

### 1. Install

```bash
git clone https://github.com/bkrabach/amplifier-hive-slack.git
cd amplifier-hive-slack
uv venv .venv && source .venv/bin/activate
uv pip install -e .
```

### 2. Run Setup

```bash
hive-slack setup
```

The setup wizard will:
1. Give you a link that creates your Slack app with everything pre-configured (one click)
2. Walk you through copying two tokens
3. Ask which AI provider you use
4. Detect WSL and suggest the right working directory
5. Write your `.env` and config files
6. Optionally install as a background service

### 3. Start Chatting

After setup, go to Slack:
1. Create a channel (e.g., `#assistant`)
2. Set the channel topic to: `[instance:assistant]`
3. Invite the bot: `/invite @Amplifier`
4. Type anything — your assistant responds

Or just DM the bot directly — no channel setup needed.

## How Conversations Work

- **Each thread is its own conversation.** The assistant remembers everything in the current thread.
- **New message in the channel = fresh start.** Like opening a new chat in ChatGPT.
- **DMs are one continuous conversation.** Like texting a person.
- **Send messages while it's working.** They get incorporated into the current task (not queued for later).

## Run as a Service

Install as a systemd service for persistent background operation:

```bash
hive-slack service install config/my-assistant.yaml
hive-slack service start
```

The service auto-restarts on crash and runs even when you're logged out.

```bash
hive-slack service status     # 🟢 running (PID 12345)
hive-slack service logs       # View recent logs
hive-slack service logs -f    # Follow live
hive-slack service restart    # After config changes
hive-slack service stop       # Stop the service
hive-slack service uninstall  # Remove entirely
```

## File Sharing

**Upload:** Drag any file into a Slack conversation. The assistant saves it to its working directory and can read, analyze, or organize it.

**Download:** When the assistant creates a file for you, it appears as an attachment in your Slack thread.

**Cloud sync:** Point the working directory at a folder inside your OneDrive or Google Drive sync folder. Files you drop there appear for the assistant automatically, and files it creates sync back to you.

## Configuration

### Config File

The setup wizard creates `config/my-assistant.yaml`:

```yaml
instance:
  name: assistant
  bundle: amplifier-dev
  working_dir: "~/Documents/Amplifier"
  persona:
    name: "Amplifier"
    emoji: ":sparkles:"

slack:
  app_token: ${SLACK_APP_TOKEN}
  bot_token: ${SLACK_BOT_TOKEN}
```

### Multiple Instances (Advanced)

For power users who want specialized assistants:

```yaml
instances:
  coder:
    bundle: amplifier-dev
    working_dir: ~/projects
    persona:
      name: Coder
      emoji: ":computer:"

  writer:
    bundle: amplifier-dev
    working_dir: ~/writing
    persona:
      name: Writer
      emoji: ":pencil:"

defaults:
  instance: coder
```

In shared channels, address a specific instance naturally: `writer: help me draft an email`

### Channel Configuration

Control channel behavior through Slack channel topics:

| Topic Directive | Behavior |
|-----------------|----------|
| `[instance:assistant]` | All messages go to this instance. Just type. |
| `[default:coder]` | Default instance, `writer: ...` to override. |
| *(no directive)* | @mention the bot required. |

### Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `SLACK_APP_TOKEN` | Yes | Socket Mode app-level token (`xapp-...`) |
| `SLACK_BOT_TOKEN` | Yes | Bot user OAuth token (`xoxb-...`) |
| `ANTHROPIC_API_KEY` | Yes* | Anthropic API key |
| `OPENAI_API_KEY` | Yes* | OpenAI API key |
| `GOOGLE_API_KEY` | Yes* | Google Gemini API key |
| `LOG_LEVEL` | No | Set to `DEBUG` for verbose output (default: `INFO`) |

*At least one AI provider key is required.

## Slack App Management

Manage the Slack app configuration programmatically:

```bash
hive-slack slack status       # View current scopes and events
hive-slack slack export       # Export manifest to file
hive-slack slack sync         # Push local manifest to Slack
```

## Architecture

```
Slack (channels, DMs, threads)
         │
    SlackConnector (Socket Mode)
      - Channel topic routing
      - Progress indicators (⏳ ⚙️ 📨)
      - File upload/download (.outbox/)
      - Message injection for busy conversations
         │
    SessionManager.execute(instance, conversation, prompt) → response
         │
    InProcessSessionManager
      - InteractiveOrchestrator (mid-execution message injection)
      - Per-conversation sessions with locking
      - Transcript persistence (JSONL)
         │
    Amplifier Core + Foundation
      - LLM providers (Anthropic, OpenAI, Gemini)
      - Tools (filesystem, bash, web, search, code intel, agents)
```

The `SessionManager` interface is the key architectural boundary. Today it runs in-process. In the future, it can be replaced by a gRPC client — with zero changes to the Slack connector.

## Slack Free Tier

This project works entirely within the Slack free plan:

- **1 app** with `chat:write.customize` for unlimited personas
- **Unlimited channels** and **unlimited members**
- **90-day message history** — sessions persist independently in JSONL
- **5 GB file storage** — store large artifacts externally, share links

See [docs/SLACK_FREE_TIER.md](docs/SLACK_FREE_TIER.md) for the complete reference.

## Development

```bash
# Install with dev dependencies
uv pip install -e ".[dev]"

# Run tests (124 tests)
pytest tests/ -v

# Check service status
hive-slack service status

# Self-test (no Slack needed)
python -m hive_slack.test_harness local
```

## Multi-Machine Setup

Run the bot on multiple machines pointed at the same Slack workspace:

1. Install on each machine
2. Use different instance names per machine
3. Each connects via Socket Mode (up to 10 connections per app)

## License

MIT
