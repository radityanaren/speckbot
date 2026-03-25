# SpeckBot 🐜

A lightweight, personal AI assistant highly inspired by [nanobot](https://github.com/HKUDS/nanobot).

**Key Features:**
- 🤖 Connect to Claude, GPT, Gemini, and any OpenAI-compatible API
- 💬 Chat via Telegram, Discord, or CLI
- 🧠 Persistent memory system with knowledge graphs
- 🛠️ Extensible with Skills and MCP servers
- ⚡ Fast, minimal dependencies

---

## Table of Contents

1. [Installation](#installation)
2. [Quick Start](#quick-start)
3. [Memory System](#memory-system)
4. [Configuration](#configuration)
5. [Commands](#commands)
6. [Interface Setup](#interface-setup)
7. [Model Setup](#model-setup)
8. [Skill Setup](#skill-setup)
9. [MCP Server Setup](#mcp-server-setup)
10. [Custom Tool Setup](#custom-tool-setup)
11. [Architecture](#architecture)

---

## Installation

### Prerequisites

- Python 3.11 or higher
- An API key for your preferred LLM provider

### Install from Source

```bash
# Clone the repository
git clone https://github.com/radityanaren/speckbot.git
cd speckbot

# Install in development mode
pip install -e ".[dev]"

# Initialize configuration
speckbot onboard --wizard
```

### Update

```bash
cd speckbot
git pull
pip install -e .
```

---

## Quick Start

### 1. Run the Setup Wizard

```bash
speckbot onboard --wizard
```

This interactive wizard will help you configure:
- LLM Provider (API key)
- Chat interface (Telegram/Discord)
- Default model and behavior

### 2. Choose Your Interface

**Run as CLI agent:**
```bash
speckbot agent
```

**Run gateway (for Telegram/Discord):**
```bash
speckbot gateway
```

---

## Memory System

SpeckBot uses a persistent memory system that survives restarts.

### Structure

```
~/.speckbot/workspace/
├── knowledges/          # General facts and knowledge
│   ├── macroeconomics/
│   │   ├── analysis.md
│   │   └── notes.md
│   └── trading-strategy/
│       └── summary.md
├── projects/            # Project-specific info
│   ├── speckbot/
│   │   └── roadmap.md
│   └── trading-bot/
│       ├── strategy.md
│       └── setup.md
├── HISTORY.md           # Conversation archive
├── SOUL.md             # Bot personality
├── AGENTS.md           # Agent instructions
└── HEARTBEAT.md        # Periodic tasks
```

### How Memory Works

1. **Knowledge Base**: Factual information (e.g., "User prefers dark mode")
2. **Project Memory**: Project-specific context (e.g., "Trading bot uses momentum indicators")
3. **Session History**: Conversation logs per channel
4. **Heartbeat Tasks**: Periodic reminders

### Saving Memories

Simply tell the bot to remember something:

```
User: "Remember that I use macOS and prefer dark mode"
Bot: I'll remember that. Should I save it as knowledge or a project?
User: Knowledge
Bot: What's the topic/folder name?
User: macos-preferences
Bot: What should I call the file?
User: display
```

Or just say it naturally:
```
User: "save this - I work on weekends only"
```

The bot will ask clarifying questions and save to:
```
knowledges/work-preferences/notes.md
```

### Viewing Memories

```
User: show my memories
Bot: 
🐜 SpeckBot Memory

📚 Knowledges:
  • macos-preferences
  • work-preferences

📁 Projects:
  • trading-bot

📜 History: present
```

### What Makes It Different

Unlike other bots that lose context when sessions end:
- ✅ **Persistent**: Memories survive restarts
- ✅ **Structured**: Organized by topic with multiple files per topic
- ✅ **Searchable**: Bot can read any memory when needed
- ✅ **Agentic**: Bot decides when to use memories based on context

---

## Configuration

All configuration lives in `~/.speckbot/config.json`. You can configure it in two ways:

| Method | Use Case |
|--------|----------|
| **Wizard** (`speckbot onboard --wizard`) | Quick setup, interactive UI |
| **Manual** (edit `~/.speckbot/config.json`) | Full control, advanced options |

### Wizard vs Manual

| Category | Wizard | Manual Config |
|----------|--------|---------------|
| **LLM Provider** | ✅ All providers | ✅ Full control |
| **Chat Channel** | ✅ Telegram, Discord | ✅ Any channel |
| **Agent Settings** | ✅ Model, temperature, tokens | ✅ All options |
| **Gateway** | ✅ Host, port, heartbeat | ✅ All options |
| **Web Tools** | ✅ Search provider, API key | ✅ Proxy, base URL |
| **Transcription** | ✅ Groq API key | ✅ - |
| **MCP Servers** | ❌ Not supported | ✅ Full config |
| **Custom Providers** | ❌ Not supported | ✅ Full config |

### Wizard Menu Options

| Option | Description |
|--------|-------------|
| `[P] LLM Provider` | Configure API keys for providers |
| `[C] Chat Channel` | Enable and configure Telegram/Discord |
| `[A] Agent Settings` | Set default model, temperature, tokens |
| `[G] Gateway` | Server host, port, heartbeat |
| `[T] Tools` | Web search, shell exec, transcription |
| `[V] View Summary` | Review current configuration |

### Complete Config Structure

Here's the full `config.json` structure (as shown by `speckbot onboard`):

```json
{
  "agents": {
    "defaults": {
      "workspace": "~/.speckbot/workspace",
      "model": "anthropic/claude-opus-4-5",
      "provider": "auto",
      "maxTokens": 8192,
      "contextWindowTokens": 65536,
      "temperature": 0.1,
      "maxToolIterations": 40,
      "reasoningEffort": null
    }
  },
  "channels": {
    "sendProgress": true,
    "sendToolHints": false,
    "discord": {
      "enabled": false,
      "token": "",
      "allowFrom": [],
      "gatewayUrl": "wss://gateway.discord.gg/?v=10&encoding=json",
      "intents": 37377,
      "groupPolicy": "mention"
    },
    "telegram": {
      "enabled": false,
      "token": "",
      "allowFrom": [],
      "proxy": null,
      "replyToMessage": false,
      "groupPolicy": "mention",
      "connectionPoolSize": 32,
      "poolTimeout": 5.0
    }
  },
  "providers": {
    "custom": {
      "apiKey": "",
      "apiBase": null,
      "extraHeaders": null
    },
    "anthropic": {
      "apiKey": "",
      "apiBase": null,
      "extraHeaders": null
    },
    "openai": {
      "apiKey": "",
      "apiBase": null,
      "extraHeaders": null
    },
    "openrouter": {
      "apiKey": "",
      "apiBase": null,
      "extraHeaders": null
    },
    "deepseek": {
      "apiKey": "",
      "apiBase": null,
      "extraHeaders": null
    },
    "ollama": {
      "apiKey": "",
      "apiBase": null,
      "extraHeaders": null
    },
    "gemini": {
      "apiKey": "",
      "apiBase": null,
      "extraHeaders": null
    }
  },
  "gateway": {
    "host": "0.0.0.0",
    "port": 18790,
    "heartbeat": {
      "enabled": true,
      "intervalS": 1800
    }
  },
  "tools": {
    "web": {
      "proxy": null,
      "search": {
        "provider": "brave",
        "apiKey": "",
        "baseUrl": "",
        "maxResults": 5
      }
    },
    "exec": {
      "timeout": 60,
      "pathAppend": ""
    },
    "transcription": {
      "groqApiKey": ""
    },
    "restrictToWorkspace": false,
    "mcpServers": {}
  }
}
```

### Config Tips

- **Wizard reads your config** - If you edit `config.json` manually, the wizard will show those values
- **Wizard writes back to config** - Changes made in wizard are saved to `config.json`
- **Press Ctrl+C to exit** - Wizard can be cancelled at any time
- **Config is JSON** - Invalid JSON will cause errors

---

## Commands

Commands are centralized in `speckbot/agent/commands.py` and handled in `speckbot/agent/loop.py`. All channels use the same commands.

### Current Commands

| Command | Description |
|---------|-------------|
| `/help` | Show available commands |
| `/memories` | View saved memories |
| `/stop` | Stop current task |
| `/restart` | Restart the bot |

### How Commands Work

All channels use **slash commands** (`/help`, `/memories`, etc.) handled **instantly** by the agent loop:

1. User sends a command (e.g., `/help`)
2. Command is recognized in `loop.py` and handled immediately (no LLM involved)
3. Response is sent back directly

### Adding a New Command

1. Add to `speckbot/agent/commands.py`:

```python
COMMANDS: dict[str, Command] = {
    # ... existing commands ...
    "mycommand": Command(
        name="/mycommand",
        description="Do something cool",
        help_text="/mycommand — Do something cool",
    ),
}
```

2. Handle in `speckbot/agent/loop.py` `_process_message()`:

```python
if cmd == "/mycommand":
    return OutboundMessage(
        channel=msg.channel,
        chat_id=msg.chat_id,
        content="Doing something cool!",
    )
```

3. Add to `TELEGRAM_BOT_COMMANDS` in `commands.py` for Telegram command menu:

---

## Interface Setup

SpeckBot supports multiple chat interfaces with **auto-discovery** — just add a channel file and it will be detected.

### Built-in Interfaces

| Interface | Protocol | Notes |
|-----------|----------|-------|
| **Telegram** | Long polling | Create via [@BotFather](https://t.me/BotFather) |
| **Discord** | Gateway websocket | Create via [Discord Developer Portal](https://discord.com/developers/applications) |
| **CLI** | Standard I/O | Just run `speckbot agent` |

### Via Wizard

```bash
speckbot onboard --wizard
```

Select **[C] Chat Channel** to:
- Enable/disable Telegram or Discord
- Enter bot tokens
- Configure group policies
- Set allowed users

### Via Config

```json
{
  "channels": {
    "telegram": {
      "enabled": true,
      "token": "123456:ABC-DEF..."
    },
    "discord": {
      "enabled": true,
      "token": "Bot xxx",
      "groupPolicy": "mention"
    }
  }
}
```

### Channel Config Options

#### Telegram Options

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `enabled` | bool | false | Enable Telegram |
| `token` | str | "" | Bot token from @BotFather |
| `allowFrom` | list | [] | Allowed user IDs (`"*"` for all) |
| `groupPolicy` | str | "mention" | `"mention"` or `"open"` |
| `proxy` | str | null | HTTP/SOCKS5 proxy URL |
| `replyToMessage` | bool | false | Reply to user messages |
| `connectionPoolSize` | int | 32 | HTTP connection pool size |
| `poolTimeout` | float | 5.0 | Pool timeout in seconds |

#### Discord Options

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `enabled` | bool | false | Enable Discord |
| `token` | str | "" | Bot token |
| `allowFrom` | list | [] | Allowed user IDs (`"*"` for all) |
| `groupPolicy` | str | "mention" | `"mention"` or `"open"` |
| `gatewayUrl` | str | "wss://..." | Discord Gateway URL |
| `intents` | int | 37377 | Gateway intents |

### Custom Interface Setup

Adding a new chat interface is automatic via auto-discovery:

#### 1. Create the Channel File

Create `speckbot/channels/mychannel.py`:

```python
from speckbot.channels.base import BaseChannel
from speckbot.shared.mixins import GroupPolicyMixin
from speckbot.config.schema import Base

class MyChannelConfig(Base):
    """Configuration for MyChannel."""
    enabled: bool = False
    token: str = ""

class MyChannel(GroupPolicyMixin, BaseChannel):
    """MyChannel implementation."""
    
    name = "mychannel"
    display_name = "MyChannel"
    
    @classmethod
    def default_config(cls) -> dict[str, Any]:
        return MyChannelConfig().model_dump()
    
    def __init__(self, config: Any, bus: MessageBus):
        if isinstance(config, dict):
            config = MyChannelConfig.model_validate(config)
        super().__init__(config, bus)
    
    @property
    def group_policy(self) -> str:
        return self.config.group_policy
    
    @property
    def bot_user_id(self) -> str | None:
        return self._user_id
    
    async def _send(self, chat_id, content, reply_params=None, thread_kwargs=None):
        # Implement channel-specific sending
        pass
```

#### 2. Add to Config

```json
{
  "channels": {
    "mychannel": {
      "enabled": true,
      "token": "xxx"
    }
  }
}
```

#### 3. Done!

Run `speckbot gateway` — the channel is auto-discovered.

### Shared Logic via Mixins

The `speckbot/shared/mixins.py` module provides shared functionality:

#### GroupPolicyMixin

For channels that support group policies (mentions):

```python
class MyChannel(GroupPolicyMixin, BaseChannel):
    @property
    def group_policy(self) -> str:
        return self.config.group_policy
    
    @property
    def bot_user_id(self) -> str | None:
        return self._user_id
    
    # Get should_respond_in_group() for free!
```

#### BaseChannel Methods to Override

| Method | Description |
|--------|-------------|
| `_send(chat_id, content)` | Send message to user |
| `_start_typing(chat_id)` | Show typing indicator |
| `_stop_typing(chat_id)` | Stop typing indicator |
| `is_allowed(sender_id)` | Check if user is allowed |
| `_should_respond_in_group()` | Check group policy |

---

## Model Setup

### Built-in Providers

| Provider | Model Prefix | Type | Notes |
|----------|-------------|------|-------|
| **OpenRouter** | Various | Gateway | Access to 100+ models |
| **Anthropic** | `anthropic/` | Direct | Claude models |
| **OpenAI** | `openai/` | Direct | GPT models |
| **DeepSeek** | `deepseek/` | Direct | DeepSeek models |
| **Gemini** | `gemini/` | Direct | Google Gemini |
| **Ollama** | `ollama/` | Local | Local models |
| **Custom** | Any | Direct | Any OpenAI-compatible API |

### Via Wizard

```bash
speckbot onboard --wizard
```

Select **[P] LLM Provider** → Choose provider → Enter API key

Then select **[A] Agent Settings** to:
- Choose model with autocomplete
- Set temperature
- Set max tokens
- Configure context window

### Via Config

```json
{
  "providers": {
    "openrouter": {
      "apiKey": "sk-or-v1-xxx",
      "apiBase": null,
      "extraHeaders": null
    }
  },
  "agents": {
    "defaults": {
      "model": "anthropic/claude-opus-4-5",
      "provider": "openrouter"
    }
  }
}
```

### Custom Provider Setup

#### 1. Add Provider Spec

Add to `speckbot/providers/registry.py`:

```python
ProviderSpec(
    name="myprovider",
    keywords=("myprovider",),
    env_key="MYPROVIDER_API_KEY",
    litellm_prefix="myprovider",
    default_api_base="https://api.myprovider.com/v1",
)
```

#### 2. Add Config Field

Add config field in `speckbot/config/schema.py`:

```python
class ProvidersConfig(Base):
    # ... existing ...
    myprovider: ProviderConfig = Field(default_factory=ProviderConfig)
```

#### 3. Use in Config

```json
{
  "providers": {
    "myprovider": {
      "apiKey": "xxx"
    }
  }
}
```

---

## Skill Setup

Skills are prompt packages that extend the agent's capabilities.

### Built-in Skills

Located in `speckbot/skills/`:

| Skill | Description |
|-------|-------------|
| **clawhub** | Search and install skills from ClawHub registry |
| **skill-creator** | Create new skills with guidance |
| **github** | GitHub integration helper |

### Via ClawHub (Natural Language)

```
User: "search for skills on clawhub"
Bot: [shows available skills]
User: "install the web-research skill"
```

Commands:
```bash
# Search
npx --yes clawhub@latest search "web scraping" --limit 5

# Install
npx --yes clawhub@latest install <slug> --workdir ~/.speckbot/workspace

# Update all
npx --yes clawhub@latest update --all --workdir ~/.speckbot/workspace

# List installed
npx --yes clawhub@latest list --workdir ~/.speckbot/workspace
```

### Manual Skill Setup

#### 1. Create Skill Directory

```bash
mkdir -p ~/.speckbot/workspace/skills/my-skill
```

#### 2. Create SKILL.md

```markdown
---
name: my-skill
description: Does something useful
---

# My Skill

Use this skill when the user asks about [topic].

Instructions for how to use this skill...
```

#### 3. Restart

Start a new session to load the skill.

### Skill Format

```markdown
---
name: skill-name
description: Brief description
---

# Skill Name

Detailed instructions for when and how to use this skill.
```

---

## MCP Server Setup

MCP (Model Context Protocol) servers add external tool capabilities.

### What MCP Adds

SpeckBot supports MCP servers for:
- **Filesystem** - Read/write files on your computer
- **GitHub** - Interact with GitHub repos
- **Brave Search** - Web search
- **Custom** - Any MCP-compatible server

### Via Wizard

❌ **Not supported** — MCP servers must be configured manually.

### Via Config

```json
{
  "tools": {
    "mcpServers": {
      "filesystem": {
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-filesystem", "/path"],
        "enabledTools": ["*"]
      }
    }
  }
}
```

### MCP Config Options

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `type` | str | null | `"stdio"`, `"sse"`, or `"streamableHttp"` (auto-detected) |
| `command` | str | "" | Command to run (e.g., `"npx"`, `"python"`) |
| `args` | list | [] | Command arguments |
| `env` | dict | {} | Environment variables |
| `url` | str | "" | HTTP endpoint for SSE/streamable |
| `headers` | dict | {} | Custom HTTP headers |
| `toolTimeout` | int | 30 | Seconds before tool call is cancelled |
| `enabledTools` | list | ["*"] | Tools to expose (`["*"]` = all) |

### Custom MCP Server Setup

#### 1. Install the MCP Server

```bash
# Filesystem access
npm install -g @modelcontextprotocol/server-filesystem

# GitHub integration
npm install -g @modelcontextprotocol/server-github

# Brave Search
npm install -g @modelcontextprotocol/server-brave-search
```

#### 2. Add to Config

```json
{
  "tools": {
    "mcpServers": {
      "my-server": {
        "command": "npx",
        "args": ["-y", "@mcp/server"],
        "enabledTools": ["*"]
      }
    }
  }
}
```

#### 3. Restart SpeckBot

```bash
speckbot gateway
```

---

## Custom Tool Setup

Tools are Python functions the agent can call during conversation.

### Built-in Tools

Located in `speckbot/agent/tools/`:

| Tool | Description |
|------|-------------|
| **web_search** | Search the web |
| **exec** | Run shell commands |
| **mcp** | Use MCP servers |
| **read_file** | Read workspace files |
| **transcribe** | Transcribe audio |

### Via Wizard

❌ **Not supported** — Custom tools require code changes.

### Via Config (Enable/Disable)

Tools are configured in config:

```json
{
  "tools": {
    "web": {
      "search": {
        "provider": "brave",
        "apiKey": "xxx",
        "maxResults": 5
      }
    },
    "exec": {
      "timeout": 60
    },
    "transcription": {
      "groqApiKey": "xxx"
    }
  }
}
```

### Custom Tool Setup

#### 1. Create Tool File

Create `speckbot/agent/tools/my_tool.py`:

```python
from speckbot.agent.tools.base import Tool, ToolResult

class MyTool(Tool):
    name = "my_tool"
    description = "Does something useful"
    parameters = {
        "type": "object",
        "properties": {
            "input": {"type": "string", "description": "The input"}
        },
        "required": ["input"]
    }

    async def execute(self, input: str) -> ToolResult:
        result = do_something(input)
        return ToolResult(content=result)
```

#### 2. Register the Tool

Add to `speckbot/agent/tools/__init__.py`:

```python
from speckbot.agent.tools.my_tool import MyTool

registry.register(MyTool())
```

#### 3. Restart SpeckBot

```bash
speckbot gateway
```

---

## Architecture

### Directory Structure

```
speckbot/
├── agent/                 # Core agent logic
│   ├── loop.py           # Main processing loop
│   ├── memory.py         # Memory system
│   ├── context.py        # Context building
│   ├── commands.py       # Command definitions (centralized)
│   └── tools/            # Built-in tools
├── bus/                  # Message passing
│   ├── events.py         # Event types
│   └── queue.py          # Async message bus
├── channels/             # Chat interfaces
│   ├── telegram.py       # Telegram implementation
│   ├── discord.py        # Discord implementation
│   ├── base.py           # Channel base class
│   └── registry.py       # Channel discovery
├── config/               # Configuration
│   ├── schema.py         # Config models
│   └── loader.py         # Config loading
├── providers/            # LLM providers
│   ├── base.py          # Provider interface
│   ├── litellm_provider.py  # LiteLLM wrapper
│   └── registry.py      # Provider registry
├── shared/               # Shared utilities
│   └── mixins.py        # Common mixins (GroupPolicyMixin, TypingIndicatorMixin)
├── skills/               # Built-in skills
│   ├── clawhub/         # ClawHub skill marketplace
│   ├── skill-creator/   # Skill creation helper
│   └── github/          # GitHub integration
└── utils/               # Utilities
    ├── constants.py     # Magic numbers
    └── helpers.py       # Helper functions
```

### How Messages Flow

```
User Message (Telegram/Discord/CLI)
         │
         ▼
┌─────────────────────────┐
│   Channel (telegram.py) │
│   - Parse message       │
│   - Check permissions   │
└───────────┬─────────────┘
            │
            ▼
┌─────────────────────────┐
│   Message Bus           │
│   - Queue message       │
│   - Route to agent      │
└───────────┬─────────────┘
            │
            ▼
┌─────────────────────────┐
│   Agent Loop            │
│   - Build context       │
│   - Call LLM            │
│   - Execute tools       │
└───────────┬─────────────┘
            │
            ▼
┌─────────────────────────┐
│   Memory System         │
│   - Check memories      │
│   - Consolidate         │
│   - Save if needed      │
└───────────┬─────────────┘
            │
            ▼
         Response
```

### Key Design Decisions

1. **Centralized Commands**: All command definitions live in `commands.py` — no duplication
2. **Provider Abstraction**: LiteLLM handles provider differences
3. **Async Channels**: Telegram and Discord use async I/O
4. **Tool Registry**: Tools self-register, easy to extend
5. **Memory as Files**: Simple filesystem storage, survives restarts
6. **Auto-Discovery**: Channels are auto-detected from `speckbot/channels/`

---

## Troubleshooting

### Bot not responding on Discord

1. Check bot is online: Look for green dot
2. Verify **Message Content Intent** is enabled
3. Ensure user is in `allowFrom` list

### Bot not responding on Telegram

1. Verify bot token is correct
2. Send any message to the bot
3. Ensure user is in `allowFrom` list

### LLM errors

1. Check API key is valid
2. Verify provider is configured correctly
3. Check model name is correct for the provider

### Memory not saving

1. Check workspace directory exists
2. Verify write permissions

---

## Contributing

Contributions welcome! Please read the architecture docs and follow the modular patterns.

---

## License

MIT
