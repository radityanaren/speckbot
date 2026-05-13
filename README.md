# SpeckBot 🐜

## About

 >  *IT'S ALIVE!* - Frankenstein 1931

Made only because I want to satisfy myself with my own agents, but come up with a cool memory system, monologue system, and sessions(I think), check it out, you might like it. Highly moddable, and modular, your agents can understand it too, fork it, abuse it, whatever you want :)

## Table of Contents
- [About](#about)
- [Features](#features)
- [Quick Install Guide](#quick-install-guide)
- [config.json](#configjson)
- [Commands](#commands)
- [Sessions](#sessions)
- [Security System](#security-system)
- [Services](#services)
- [Skills](#skills)
- [Tools](#tools)
- [Channels](#channels)
- [Providers](#providers)
- [Structure](#structure)

## Features

- **Extensible** : Highly moddable and extensible features such as tools, channels, providers
- **Enhanced Memory** : Conveyor Belt memory style, indexed knowledges and projects based memory, fuzzysearch memory index etc
- **Security Detectors** : Block regex patterns, ASK-before-execution for dangerous tools
- **Background services** : Idle monologue, dream, cron jobs, heartbeat
- **MCP support** : Connect any Model Context Protocol server at runtime
- **Skills system** : Drop in SKILL.md files to teach the agent new capabilities

## Quick Install Guide

### Download and Install

```bash
git clone https://github.com/radityanaren/speckbot
cd speckbot
pip install -e .
```

> [!WARNING]
> **Windows Users:** You need to install [Git Bash](https://git-scm.com/download/win) to use the bash tool.

### Configuration

1. **Generate config and workspace:**
   ```bash
   speckbot onboard
   ```
   This creates `config.json`, `.env`, and the workspace directory.

2. **Add secrets to `.env`:**
   ```
   OPENROUTER_API_KEY=sk-or-...
   TELEGRAM_TOKEN=12345:ABC...
   ```

3. **Edit `config.json`:** Reference secrets with `${VAR_NAME}` syntax:
   ```json
   {
     "agents": {
       "defaults": {
         "provider": "provider_a",
         "workspace": "~/.speckbot/workspace"
         "projects_root": "~/.speckbot/workspace/projects"
       }
     },
     "providers": [
       {
         "name": "provider_a",
         "apiKey": "${OPENROUTER_API_KEY}",
         "apiBase": "https://openrouter.ai/api/v1",
         "model": "anthropic/claude-sonnet-4-5"
       }
     ],
     "channels": {
       "telegram": {
         "enabled": true,
         "token": "${TELEGRAM_TOKEN}"
       }
     }
   }
   ```

4. **Run:**
   ```bash
   speckbot gateway
   ```

## config.json

The root config object uses Pydantic and supports `${VAR}` interpolation from `.env` files (default: same directory as `config.json`).

### Provider Types

Each provider in the `providers` list has a `type`:

| Type | Description |
|------|-------------|
| `"custom"` | OpenAI-compatible endpoint (direct) |
| `"litellm"` | LiteLLM-backed — auto-routes to any provider via model string |

### Adding Multiple Providers

```json
{
  "providers": [
    {
      "name": "provider_a",
      "type": "litellm",
      "apiKey": "${OPENROUTER_API_KEY}",
      "apiBase": "https://openrouter.ai/api/v1",
      "model": "anthropic/claude-sonnet-4-5"
    },
    {
      "name": "provider_b",
      "type": "custom",
      "apiKey": "${LOCAL_API_KEY}",
      "apiBase": "http://localhost:8000/v1",
      "model": "local-model"
    }
  ]
}
```

Set the active provider in `agents.defaults.provider`.

### Project Tracking

Set `agents.defaults.projects_root` to your projects folder (default: `~/.speckbot/workspace/projects`).
The agent tracks projects via `SPECKBOT.md` inside each project folder.
Dream scans for `SPECKBOT.md` files and rebuilds MEMORY.md index.

```json
{
  "agents": {
    "defaults": {
      "projects_root": "~/.speckbot/workspace/projects"
    }
  }
}
```

### Key Sections

| Key | Description |
|-----|-------------|
| `agents.defaults` | Active provider, token limits, temperature, iterations, projects_root |
| `providers[]` | List of configured LLM backends |
| `channels` | Per-channel config (telegram, discord, etc.) |
| `tools` | Web search, bash exec, MCP servers, workspace restriction |
| `services` | Heartbeat, monologue, cron, dream settings |
| `security` | Block patterns, ASK tools, audit log |
| `gateway` | Host/port for WebSocket gateway |

## Commands

### CLI Commands

These are Typer commands available via `speckbot <command>`:

| Command | Description |
|---------|-------------|
| `onboard` | Initialize config and workspace (`speckbot onboard`) |
| `gateway` | Start the SpeckBot gateway (`speckbot gateway`) |
| `status` | Show config, workspace, and provider status |

Add custom CLI commands by adding `@app.command()` functions in `speckbot/cli/commands.py`.

### Bot Commands

These are chat commands (type `/command` in Telegram, Discord, or CLI):

| Command | Description |
|---------|-------------|
| `/new` | Clear session, archive existing messages |
| `/flush` | Compact oldest 90% via LLM, keep newest 10% |
| `/memories` | List saved knowledges and projects |
| `/stop` | Stop the current task |
| `/restart` | Restart SpeckBot |
| `/help` | Show available commands |

Add custom bot commands by adding entries to `_COMMANDS` in `speckbot/agent/definitions.py`.

## Sessions

SpeckBot uses a **conveyor belt** session system — messages flow through three layers:

1. **Session messages** — Recent conversation in active context
2. **Summary lines** — Compressed context shown in system prompt
3. **JSONL archive** — Full archived messages stored on disk

### How Compaction Works

Two-step archiving triggers when the estimated prompt exceeds token thresholds:

- **Step 1** (soft clip): When prompt > `active_window_tokens × tool_truncation_percent%` — archives the most recent tool call block (assistant tool_calls + results) and summarizes it
- **Step 2** (hard clip): When prompt > `active_window_tokens × 105%` — clips oldest lines regardless of source (messages + summaries) until under target

### Session Storage

Sessions are stored as JSONL in `<workspace>/sessions/`. Archives go to `<workspace>/archive/`.

### Config Controls

```json
{
  "active_window_tokens": 65536,
  "tool_truncation_percent": 50,
  "summary_result_max_chars": 100,
  "summary_assistant_max_chars": 150
}
```

### Commands

- `/new` — Clear session, archive existing messages
- `/flush` — Compact oldest 90% via LLM, keep newest 10%
- `/memories` — List saved knowledges and projects

## Security System

SpeckBot has a two-layer security gateway that runs on input, tool calls, and output.

### BLOCK — Filter Patterns

Regex patterns that silently block messages or tool output.

```json
{
  "security": {
    "enabled": true,
    "blocked_patterns": ["SSN_REGEX_HERE", "API_KEY_PATTERN"]
  }
}
```

When blocked:
- User input → "Your message was blocked by security filters"
- Tool output → "[Output filtered by security]"
- AI output → "[BLOCKED - sensitive content]"

### ASK — User Confirmation

Tools in `ask_tools` require explicit `yes`/`no` from the user before execution. This blocks ALL other processing until resolved.

```json
{
  "security": {
    "enabled": true,
    "ask_tools": ["edit_file", "write_file", "bash"]
  }
}
```

Default ask tools: `edit_file`, `write_file`, `bash`.

### How It Works

1. **Input scan** — User message checked against `blocked_patterns`
2. **Tool scan** — Before execution, tool name + params checked
3. **Output scan** — AI response scanned before sending to user
4. **Tool result scan** — Tool output scanned before AI sees it

### Adding Custom Detectors

Create a detector in `speckbot/security/detectors/` extending `DetectorBase`:
```python
class MyDetector(DetectorBase):
    def scan(self, text: str) -> SecurityResult:
        # Return SecurityResult(is_blocked=True, reason="...") if matched
```

## Services

### Monologue

Self-talk when idle. After `monologue_idle_seconds` of no user messages, the agent sends itself a prompt and responds (like talking to itself). If it wants to message you, it uses the `message` tool.

```json
{
  "services": {
    "monologue_enabled": true,
    "monologue_idle_seconds": 300,
    "monologue_prompt": "Hey, been a while — what are you working on?",
    "monologue_visible": true
  }
}
```

### Dream

Daily memory cleanup. Runs at startup and every `dream_sleep_interval_hours`:

1. For each session, archives oldest 90% via LLM consolidation
2. Restarts the SpeckBot process

```json
{
  "services": {
    "dream_enabled": true,
    "dream_max_memory_lines": 200,
    "dream_deduplicate": true,
    "dream_convert_dates": true,
    "dream_sleep_interval_hours": 24
  }
}
```

### Cron

Schedule tasks that run through the agent loop. Use the `cron` tool to create/manage jobs:

```python
# Via agent: cron(action="create", name="daily-report", schedule="0 9 * * *", message="Generate daily summary")
```

```json
{
  "services": {
    "cron_enabled": true
  }
}
```

### Heartbeat

Periodic check-in that executes configured tasks through the agent and optionally delivers the response to a channel.

```json
{
  "services": {
    "heartbeat_enabled": true,
    "heartbeat_interval_seconds": 1800
  }
}
```

## Skills

Skills are `SKILL.md` files that teach the agent capabilities. They support metadata frontmatter for requirements and "always-on" flag.

### Adding Skills

1. **Drop into workspace:** Create `<workspace>/skills/<name>/SKILL.md`
2. **Via the agent:** Use `save_knowledge` / `save_project` tools
3. **Built-in:** Shipped in `speckbot/skills/`

### SKILL.md Format

```markdown
---
description: "What this skill does"
always: false
metadata: |
  {
    "speckbot": {
      "requires": {
        "bins": ["ffmpeg"],
        "env": ["OPENAI_API_KEY"]
      }
    }
  }
---

Skill instructions go here...
```

- `always: true` — Auto-included in system prompt every turn
- `requires.bins` — CLI tools that must be on PATH
- `requires.env` — Environment variables that must be set

## Tools

### Python Tools

| Tool | Description |
|------|-------------|
| `read_file` | Read file contents |
| `write_file` | Write/create file |
| `edit_file` | Edit file using diff |
| `list_dir` | List directory contents |
| `bash` | Execute shell command |
| `web_search` | Search the web (Brave) |
| `web_fetch` | Fetch URL content |
| `message` | Send message to user |
| `spawn` | Spawn a background subagent |
| `cron` | Manage scheduled tasks |
| `save_knowledge` | Save notes to knowledges |
| `save_project` | Save SPECKBOT.md to a project folder |
| `list_memories` | List all knowledges and projects |
| `fuzzy_search_memory` | Fuzzy search MEMORY.md (handles typos) |

### Adding Custom Python Tools

1. Create `speckbot/tools/mytool.py`:
   ```python
   from speckbot.tools.base import Tool

   class MyTool(Tool):
       @property
       def name(self): return "my_tool"
       @property
       def description(self): return "Does something"
       @property
       def parameters(self): return {"type": "object", "properties": {...}}
       async def execute(self, **kwargs): return "result"
   ```

2. Register in `AgentLoop._register_default_tools()`:
   ```python
   self.tools.register(MyTool())
   ```

### MCP Servers

MCP servers are configured in `config.json`. Empty by default — add your own:

```json
{
  "tools": {
    "mcp_servers": {
      "my-server": {
        "type": "stdio",
        "command": "npx",
        "args": ["@some/mcp-server"],
        "env": {},
        "tool_timeout": 30,
        "enabled_tools": ["*"]
      }
    }
  }
}
```

**Server types:** `stdio`, `sse`, `streamableHttp`

Connections are lazy — established on first message, not at startup.

## Channels

### Existing Channels

| Channel | Config Key | Transport |
|---------|-----------|-----------|
| Telegram | `telegram` | Long polling |
| Discord | `discord` | Gateway WebSocket |

### Channel Config

```json
{
  "channels": {
    "telegram": {
      "enabled": true,
      "token": "${TELEGRAM_TOKEN}",
      "allow_from": ["*"],
      "proxy": null,
      "reply_to_message": false,
      "group_policy": "mention"
    }
  }
}
```

### Adding Custom Channels

1. Create `speckbot/bus/channels/mychannel.py`:
   ```python
   from speckbot.bus.channels.base import BaseChannel

   class MyChannel(BaseChannel):
       name = "mychannel"
       display_name = "My Channel"

       async def start(self): ...
       async def stop(self): ...
       async def send(self, msg: OutboundMessage): ...
   ```

2. Register in `speckbot/bus/channels/registry.py` or use entry_points for plugins.

## Providers

### Existing Providers

SpeckBot supports any provider LiteLLM supports, plus direct OpenAI-compatible endpoints:

| Type | Examples |
|------|----------|
| `"litellm"` | `anthropic/claude-*`, `openai/gpt-*`, `gemini/*`, etc. |
| `"custom"` | Any OpenAI-compatible endpoint (local LLM, vLLM, etc.) |

### Provider Config

```json
{
  "providers": [
    {
      "name": "my-provider",
      "type": "litellm",
      "apiKey": "${API_KEY}",
      "apiBase": "https://api.example.com/v1",
      "model": "provider/model-name",
      "extra_headers": {}
    }
  ]
}
```

### Adding Custom Provider Classes

For providers needing special handling, create a subclass in `speckbot/providers/` and set `"type"` to the class name.

## Structure

After reorganization, the codebase is organized as:

```
speckbot/
├── agent/              # Core orchestration
│   ├── loop.py         # AgentLoop + MessageHandler
│   ├── context.py      # System prompt + message builder
│   ├── subagent.py     # Background subagent spawning
│   ├── security.py     # Security service wrapper
│   └── definitions.py  # Help text, bot commands
├── bus/                # Message routing
│   ├── events.py       # InboundMessage, OutboundMessage
│   ├── queue.py        # Async message queues
│   └── channels/       # Telegram, Discord, CLI
├── cli/                # Command-line interface
│   ├── commands.py     # Typer CLI (onboard, gateway, status)
│   └── templates/      # Workspace templates (AGENTS.md, etc.)
├── config/             # Configuration
│   ├── schema.py       # Pydantic models
│   ├── loader.py       # Config loading + .env interpolation
│   └── paths.py        # Path helpers
├── providers/          # LLM backends
│   ├── base.py         # LLMProvider interface
│   ├── litellm_provider.py
│   ├── custom_provider.py
│   └── registry.py     # Provider specs
├── security/           # Security system
│   ├── __init__.py     # SecurityGateway
│   └── detectors/      # BlockDetector, AskDetector
├── services/           # Background services
│   ├── timer.py        # UnifiedTimer (coordinates all)
│   ├── monologue/      # Idle self-talk
│   ├── heartbeat/      # Periodic check-in
│   ├── cron/           # Scheduled tasks
│   └── dream/          # Daily memory cleanup
├── session/            # Conversation state
│   ├── manager.py      # Session, SessionManager
│   └── memory.py       # MemoryConsolidator, MemoryStore
├── skills/             # Agent skills
│   ├── __init__.py     # SkillsLoader
│   └── <skill dirs>    # SKILL.md files
├── tools/              # Tool implementations
│   ├── base.py         # Tool abstract class
│   ├── registry.py     # ToolRegistry
│   ├── bash.py
│   ├── filesystem.py
│   ├── web.py
│   ├── message.py
│   ├── spawn.py
│   ├── cron.py
│   └── mcp.py          # MCP client
└── utils/              # Shared utilities
    ├── helpers.py
    ├── constants.py
    └── evaluator.py
```
