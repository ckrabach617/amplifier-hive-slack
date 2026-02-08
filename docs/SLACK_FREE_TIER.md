# Slack Free Plan Constraints

Reference for what we can and can't do on the free tier. Updated February 2026.

## Showstoppers (Must Architect Around)

| Constraint | Limit | Our Mitigation |
|------------|-------|----------------|
| App installations | **10 max** | Single app + `chat:write.customize` for multiple personas |
| Message history | **90 days visible**, **1 year then permanently deleted** | Session persistence in JSONL (our own storage) |
| File storage | **5 GB total workspace** | Store artifacts externally, share links |
| Workflow Builder | **Not available** | All orchestration in our backend |
| Compliance export | **Not available** (can't export private channels/DMs) | Our own persistence layer is the archive |

## Important But Manageable

| Constraint | Limit | Notes |
|------------|-------|-------|
| Message posting rate | **1 msg/sec/channel** | Queue messages; distribute across channels |
| Message length | **4,000 chars recommended, 40,000 hard truncation** | Split long outputs or use file uploads |
| Blocks per message | **50 blocks** | Chunk rich content across messages |
| Guest accounts | **Not available** | External users must be full workspace members |
| SSO/SAML | **Not available** | Password-only auth |
| Custom user groups | **Not available** | Can't create @team-style mentions |
| Channel posting restrictions | **Only #general** | Can't restrict posting in other channels |

## Non-Issues (Generous or Unlimited)

| Feature | Limit | Notes |
|---------|-------|-------|
| Channels | **Unlimited** | Create as many as needed |
| Workspace members | **Unlimited** | No user cap |
| Event delivery | **30,000 events/hour/app** | More than sufficient |
| Socket Mode connections | **10 per app** | Generous for internal use |
| API rate tiers | **Same as paid plans** (for internal apps) | Tier 3: 50+ req/min |
| Custom emoji | **Effectively unlimited** | Use for agent status, reactions |
| Audio/video clips | **Available** | |

## API Rate Limits (Internal Apps)

| Tier | Rate | Key Methods |
|------|------|-------------|
| Tier 1 | 1+/min | `admin.*`, `dnd.*` |
| Tier 2 | 20+/min | `conversations.list`, `channels.info` |
| Tier 3 | 50+/min | `conversations.history`, `conversations.replies`, `conversations.info` |
| Tier 4 | 100+/min | Various read methods |
| Special | 1 msg/sec/channel | `chat.postMessage` |

**Note:** The May 2025 rate limit changes for `conversations.history`/`conversations.replies` only affect commercially distributed non-Marketplace apps. Internal custom apps (like ours) retain full Tier 3 rates.

## Key for Multi-Agent Architecture

- One app, unlimited personas via `chat:write.customize`
- Socket Mode for local hosting (no public URL)
- External persistence for anything beyond 90 days
- Request queuing with `Retry-After` header handling
- File links instead of uploads to conserve 5 GB storage
