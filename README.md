# SpeckBot 🤖

A lightweight, personal AI agent with a robust memory system.

## What is SpeckBot?

SpeckBot is your personal AI assistant that runs locally and connects to your favorite chat platforms. It features:

- **Multi-Channel**: Telegram, Discord, CLI
- **Tool Execution**: Read/write files, run commands, search the web, and more
- **Persistent Memory**: Conversations are archived automatically, with summary context preserved
- **Security**: Block sensitive data, confirm dangerous operations
- **Flexible Providers**: OpenRouter, Anthropic, OpenAI, NVIDIA NIM, and many more

## Quick Start

### 1. Install

```bash
pip install speckbot-ai
# or
git clone https://github.com/radityanaren/speckbot
cd speckbot
pip install -e .
```

### 2. Initialize

```bash
speckbot onboard
```

This creates:
- `config.json` - Main configuration
- `.env` - API keys and secrets
- `workspace/` - Working directory

### 3. Configure

Edit `config.json` to add your provider and channels:

```json
{
  "agents": {
    "defaults": {
      "provider": "provider_a",
      "workspace": "~/.speckbot/workspace"
    }
  },
  "providers": [
    {
      "name": "provider_a",
      "apiKey": "${YOUR_API_KEY}",
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

Add your keys to `.env`:

```
OPENROUTER_API_KEY=sk-or-...
TELEGRAM_TOKEN=12345:ABC...
```

### 4. Run

```bash
speckbot gateway
```

## Commands

| Command | Description |
|---------|-------------|
| `/new` | Start a new session |
| `/help` | Show help |
| `/memories` | List saved knowledges and projects |
| `/flush` | Compact conversation history |
| `/stop` | Stop running tasks |
| `/restart` | Restart the bot |

## Tools

Built-in tools available to the agent:

| Tool | Description |
|------|-------------|
| `read_file` | Read file contents |
| `write_file` | Write/create file |
| `edit_file` | Edit file using diff |
| `list_dir` | List directory contents |
| `bash` | Execute shell command |
| `web_search` | Search the web |
| `web_fetch` | Fetch URL content |
| `message` | Send message to user |
| `spawn` | Spawn a subagent |
| `cron` | Manage scheduled tasks |

## Memory System

SpeckBot maintains three layers of memory:

1. **Session** - Full message history in context
2. **Summary** - Summarized lines in system prompt
3. **Archive** - Old messages in JSONL files

When the conversation gets too long, oldest messages are archived automatically. The summary preserves context across archives.

### Saving Memories

The agent can save memories using:

- `save_knowledge(topic, content, file_type)` - Save factual knowledge
- `save_project(topic, content, file_type)` - Save project context
- `list_memories()` - List all saved memories

## Configuration

### Agent Settings

```json
{
  "agents": {
    "defaults": {
      "workspace": "~/.speckbot/workspace",
      "provider": "provider_a",
      "max_output_tokens": 8192,
      "active_window_tokens": 65536,
      "tool_truncation_percent": 50,
      "tool_result_max_chars": 10000,
      "max_tool_iterations": 40,
      "temperature": 0.7
    }
  }
}
```

### Tools Settings

```json
{
  "tools": {
    "web_search_provider": "brave",
    "web_search_api_key": "${BRAVE_API_KEY}",
    "exec_timeout": 60,
    "restrict_to_workspace": false,
    "mcp_servers": {}
  }
}
```

### Services Settings

```json
{
  "services": {
    "heartbeat_enabled": true,
    "heartbeat_interval_seconds": 1800,
    "monologue_enabled": false,
    "monologue_idle_seconds": 300,
    "monologue_prompt": "Hey, been a while — what are you working on?",
    "cron_enabled": true,
    "dream_enabled": false,
    "dream_sleep_interval_hours": 24
  }
}
```

### Security Settings

```json
{
  "security": {
    "enabled": false,
    "blocked_patterns": [],
    "ask_tools": ["edit_file", "write_file", "exec"],
    "audit_log": null
  }
}
```

## Workspace

The workspace directory contains:

```
workspace/
├── sessions/           # Conversation sessions (JSONL)
├── archive/            # Archived messages
├── knowledges/         # Saved knowledge (topic/notes.md)
├── projects/          # Saved project context
├── cron/               # Scheduled tasks
├── AGENTS.md           # Agent instructions
├── MEMORY.md           # Memory system index
└── JOURNAL.md          # Inner monologue journal
```

## Development

### Running Tests

```bash
pytest
```

### Code Style

```bash
ruff check speckbot/
ruff format speckbot/
```

## License

MIT

## Links

- [GitHub](https://github.com/radityanaren/speckbot)
- [Issues](https://github.com/radityanaren/speckbot/issues)