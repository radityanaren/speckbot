# SpeckBot Code Architecture

> This document describes SpeckBot's architecture so the code itself can understand. Written for developers and for AI-assisted maintenance.

## Overview

SpeckBot is a lightweight, multi-channel AI agent with a robust memory system. It receives messages from chat platforms (Telegram, Discord, CLI), processes them through an LLM with tool execution, and returns responses.

```
┌─────────────────────────────────────────────────────────────┐
│                    CHANNELS                                │
│  TelegramChannel │ DiscordChannel │ CLI (stdin/stdout)       │
└─────────────────────────┬───────────────────────────────────┘
                        │ InboundMessage
                        ▼
┌─────────────────────────────────────────────────────────────┐
│                    MESSAGE BUS                             │
│         (AsyncQueue<InboundMessage>,                     │
│          AsyncQueue<OutboundMessage>)                    │
└─────────────────────────┬───────────────────────────────────┘
                        ▼
┌─────────────────────────────────────────────────────────────┐
│                    AGENT LOOP                             │
│  ┌─────────────────────────────────────────────────────┐   │
│  │         ContextBuilder                                │   │
│  │   Builds system prompt + message history            │   │
│  └─────────────────────────────────────────────────────┘   │
│  ┌─────────────────────────────────────────────────────┐   │
│  │         ToolRegistry + Tools                      │   │
│  │   bash, web_search, read_file, write_file, etc.    │   │
│  └─────────────────────────────────────────────────────┘   │
│  ┌─────────────────────────────────────────────────────┐   │
│  │         MemoryConsolidator                       │   │
│  │   Archives old messages, maintains context       │   │
│  └─────────────────────────────────────────────────────┘   │
│                         │                                │
│                         ▼ LLM Request                   │
│  ┌─────────────────────────────────────────────────────┐   │
│  │         LLM Provider (LiteLLM)                    │   │
│  │   OpenRouter, Anthropic, OpenAI, NVIDIA, etc.     │   │
│  └──────────��──────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────┘
                        │ OutboundMessage
                        ▼
┌─────────────────────────────────────────────────────────────┐
│                    CHANNELS (send)                        │
└─────────────────────────────────────────────────────────────┘
```

## Core Components

### 1. Message Bus (`speckbot/bus/`)

The message bus decouples channels from the agent. Two queues:

- **inbound**: Channels push `InboundMessage` here
- **outbound**: Agent pushes `OutboundMessage` here

```python
# InboundMessage - received from chat platform
@dataclass
class InboundMessage:
    channel: str          # "telegram", "discord", "cli"
    sender_id: str         # User identifier
    chat_id: str          # Chat/channel identifier
    content: str          # Message text
    media: list[str]      # Attached media paths
    metadata: dict        # Channel-specific (message_id, user_id, etc.)
    session_key: str       # Computed: channel:chat_id (or thread-scoped)
```

```python
# OutboundMessage - to send to chat platform
@dataclass
class OutboundMessage:
    channel: str
    chat_id: str
    content: str
    media: list[str]
    metadata: dict
    progress_type: "thought" | "tool_hint" | None  # For streaming updates
```

### 2. Agent Loop (`speckbot/agent/loop.py`)

The orchestration center. Main methods:

- `run()` - Main loop: consumes inbound messages, dispatches to `_dispatch()`
- `_dispatch(msg)` - Processes one message under global lock
- `_process_message(msg)` - Delegates to MessageHandler
- `_run_agent_loop(messages)` - Core LLM iteration: chat → execute tools → repeat
- `_handle_flush(msg)` - Compacts oldest 90% via LLM, keeps newest 10%
- `_handle_stop(msg)`, `_handle_restart(msg)` - Control commands

Security integration:
- `_pending_confirmation` - One pending confirmation per session
- `set_pending_confirmation()` - Ask system: require user yes/no before dangerous tools
- `get_pending_confirmation()` - Check if waiting for confirmation
- Security scan on tool output before adding to LLM context

### 3. Message Handler (`speckbot/agent/loop.py`)

Extracted from AgentLoop for clean separation. Handles:

- `process(msg)` - Main entry: checks pending confirmation, routes to handler
- `_handle_user_message(msg)` - Normal user messages
- `_handle_system_message(msg)` - Messages from subagents
- `_handle_slash_command(msg, cmd)` - `/new`, `/help`, `/memories`
- `_save_turn(session, messages, skip)` - Saves new-turn messages to session

### 4. Context Builder (`speckbot/agent/context.py`)

Builds the full prompt for each LLM call:

- `build_system_prompt()` - Identity + runtime + bootstrap files + skills
- `build_messages(history, current_message, ...)` - Full message list
- `_build_runtime_context(channel, chat_id, ...)` - Metadata block for injection
- `_build_user_content(text, media)` - Text + base64-encoded images/videos

Bootstrap files (loaded from workspace):
- `AGENTS.md` - Agent instructions
- `MEMORY.md` - Memory system index
- `JOURNAL.md` - Inner monologue journal

### 5. Session & Session Manager (`speckbot/session/manager.py`)

**Session** - One conversation:

```python
@dataclass
class Session:
    key: str                              # channel:chat_id
    messages: list[dict]                 # Full message history
    created_at: datetime
    updated_at: datetime
    summary_lines: list[str]            # Summarized context
    last_archived: int                   # Legacy: index of first unarchived
    metadata: dict                      # Extra data
```

- `get_history(max_messages, active_window_tokens)` - Returns messages for LLM
- `append_summary(line)` - Adds summary line
- `get_context_summary()` - Returns `<context-summary>` XML block
- `clear()` - Resets session

**SessionManager** - Manages all sessions:

- `get_or_create(key)` - Gets or creates session (in-memory cache + disk load)
- `save(session)` - Saves to JSONL file
- `archive_session(session)` - Archives old messages to JSONL
- `read_archive(...)` - Reads from archive
- `list_sessions()` - Lists all sessions

Session storage format (JSONL):
```
{"_type": "metadata", "key": "telegram:123", "created_at": "...", ...}
{"_type": "summary", "content": "[11:09] USER: hello"}
{"role": "user", "content": "hello", "timestamp": "..."}
{"role": "assistant", "content": "Hi!", "timestamp": "..."}
```

### 6. Memory System (`speckbot/agent/memory.py`)

Three-layer memory:

**Layer 1: Session Messages** - Full conversation in session.messages

**Layer 2: Summary Lines** - `session.summary_lines`, displayed in system prompt

**Layer 3: Archive** - JSONL files in `workspace/archive/`

**MemoryConsolidator** - Manages archiving:

- `maybe_archive_by_tokens(session)` - Archives when prompt size exceeds threshold
- `estimate_session_prompt_tokens(session)` - Estimates current prompt size
- Uses two-step approach:
  - **Step 1**: Archive tool call blocks when `estimated > active_window_tokens * tool_truncation_percent%`
  - **Step 2**: Clip oldest lines when `estimated > active_window_tokens * 105%`

**MemoryStore** - Persistent storage for agent:

- `knowledges/` - Factual/technical knowledge folders
- `projects/` - Project-specific context folders

Tools: `save_knowledge`, `save_project`, `list_memories`

### 7. Tool Registry & Tools (`speckbot/agent/tools/`)

**ToolRegistry** - Manages tool registration and execution:

- `register(tool)` - Registers a tool
- `get(name)` - Gets tool by name
- `execute(name, params, session_key)` - Executes tool
- `get_definitions()` - Returns OpenAI-format tool schemas

**Base Tool** - Abstract base:

```python
class Tool:
    name: str
    description: str
    
    async def execute(**params) -> str:
        ...
    
    def to_schema() -> dict:  # OpenAI format
        ...
    
    def cast_params(params: dict) -> dict:
        ...
    
    def validate_params(params: dict) -> list[str]:
        ...
```

**Built-in Tools**:

| Tool | Description | Key Parameters |
|------|-------------|---------------|
| `read_file` | Read file contents | `path` |
| `write_file` | Write file | `path`, `content` |
| `edit_file` | Edit file (diff) | `path`, `old Text`, `new Text` |
| `list_dir` | List directory | `path` |
| `bash` | Execute shell command | `command` |
| `web_search` | Search web (Brave) | `query`, `num_results` |
| `web_fetch` | Fetch URL content | `url` |
| `message` | Send message to user | `content` |
| `spawn` | Spawn subagent | `prompt`, `model` |
| `cron` | List/create scheduled tasks | `action`, `name`, `schedule`, `message` |

**MCP Tools** - Support for Model Context Protocol servers:

- `connect_mcp_servers(servers, tool_registry, stack)` - Connects MCP servers
- Lazy connection on first message

### 8. LLM Provider (`speckbot/providers/`)

**LiteLLMProvider** - Uses LiteLLM for multi-provider support:

- Auto-detects provider from model string
- Handles OpenAI-format messages
- Provider registry (`speckbot/providers/registry.py`) defines:
  - Model prefixes (e.g., `anthropic/`, `openai/`)
  - Environment variables
  - Special parameters
  - Prompt caching support

Key methods:
- `chat(messages, tools, model, ...)` - Send chat completion
- `_parse_response(response)` - Parse LiteLLM response
- `_resolve_model(model)` - Apply provider prefixes
- `_sanitize_messages(messages)` - Strip non-standard keys

### 9. Services (`speckbot/services/`)

**UnifiedTimer** - Coordinates all timing:

- Single 1-second tick loop
- Triggers heartbeat, monologue, dream

**HeartbeatService** - Periodic check-ins:

- `on_execute(tasks)` - Execute tasks through agent
- `on_notify(response)` - Deliver response to user

**MonologueSystem** - Self-talk when idle:

- Config: `idle_seconds`, `prompt`, `visible`
- `handle_idle(callback)` - Triggered after idle
- `on_user_message()` - Reset idle counter

**DreamEngine** - Daily memory cleanup:

- Runs at startup + every `sleep_interval_hours`
- Archives oldest 90% via LLM consolidation
- Restarts process

**CronService** - Scheduled tasks:

- `add_job(job)`, `remove_job(job_id)`, `list_jobs()`
- `on_job` callback executes through agent

### 10. Security (`speckbot/security/`)

**SecurityGateway** - Security detectors:

- `BlockDetector` - Block sensitive data patterns
- `AskDetector` - Require user confirmation for dangerous tools

Flow:
1. `scan_input(text)` - Check user message for blocked patterns
2. `scan_tool(name, params, session_key)` - Check tool call
3. `scan_output(text)` - Check AI output
4. `scan_tool_output(text)` - Check tool result

Config:
- `blocked_patterns` - Regex patterns to block
- `ask_tools` - Tools requiring confirmation (`edit_file`, `write_file`, `exec`, etc.)
- `audit_log` - Optional audit log file

### 11. Channels (`speckbot/channels/`)

**ChannelManager** - Manages all channels:

- `start_all()` - Starts enabled channels
- `stop_all()` - Stops all channels

**BaseChannel** - Abstract channel:

```python
class BaseChannel(ABC):
    name: str
    display_name: str
    
    async def start():
        ...
    
    async def stop():
        ...
    
    async def send(msg: OutboundMessage):
        ...
    
    def is_allowed(sender_id: str) -> bool:
        ...
```

**TelegramChannel** - Long polling:
- Download media (photo, voice, document)
- Markdown to HTML conversion
- Media groups aggregation
- Typing indicators
- Topic-scoped sessions

**DiscordChannel** - WebSocket:
- Similar features to Telegram

## Data Flow

### 1. Message Processing

```
InboundMessage (from channel)
    │
    ▼
AgentLoop._dispatch(msg)
    │
    ├──▶ MessageHandler.process(msg)
    │       │
    │       ├──▶ Check pending confirmation
    │       │           │
    │       │           ├──▶ "yes" → execute tool, clear confirmation
    │       │           ├──▶ "no" → clear confirmation
    │       │           └──▶ other → ask again
    │       │
    │       ├──▶ /new, /help, /memories →slash command
    │       │
    │       └──▶ _handle_user_message(msg)
    │               │
    │               ├──▶ session.get_history()
    │               ├──▶ ContextBuilder.build_messages()
    │               │
    │               └──▶ AgentLoop._run_agent_loop()
    │                       │
    │                       ├──▶ Provider.chat(messages, tools)
    │                       │       │
    │                       │       └──▶ LLMResponse(content, tool_calls)
    │                       │
    │                       ├──▶ If tool_calls:
    │                       │       │
    │                       │       ├──▶ Security: check if ask_tool
    │                       │       │       │
    │                       │       │       └──▶ If ask: set pending, ask user
    │                       │       │
    │                       │       └──▶ ToolRegistry.execute(tool, params)
    │                       │               │
    │                       │               └──▶ Tool.execute(**params)
    │                       │                       │
    │                       │                       └──▶ Security: scan output
    │                       │
    │                       └──▶ If no tool_calls: return content
    │
    ├──▶ Save to session (MessageHandler._save_turn)
    ├──▶ Schedule memory consolidation
    └──▶ Return OutboundMessage
```

### 2. Memory Consolidation

```
maybe_archive_by_tokens(session)
    │
    ├──▶ estimate_session_prompt_tokens(session)
    │
    ├──▶ If estimated > step1_threshold:
    │       │
    │       └──▶ _archive_tool_blocks(session)
    │               │  (Archive most recent tool call + results)
    │               │
    │               ├──▶ Summarize tool block
    │               ├──▶ Write to JSONL archive
    │               └──▶ Remove from session.messages
    │
    └──▶ If estimated > step2_threshold:
            │
            └──▶ _archive_all_with_hardclip(session, estimated)
                    │  (Clip oldest lines regardless of source)
                    │
                    ├──▶ Build timeline (messages + summary_lines)
                    ├──▶ Sort by timestamp
                    ├──▶ Archive oldest
                    └──▶ Rebuild session
```

### 3. Tool Execution Security

```
ToolRegistry.execute(name, params)
    │
    ├──▶ Security.scan_tool(name, params)
    │       │
    │       └──▶ If blocked: return error
    │
    ├──▶ tool.execute(**params)
    │
    └──▶ Security.scan_output(result)
            │
            └──▶ If blocked: return "[Output filtered]"
```

## Configuration

### Config Schema (`speckbot/config/schema.py`)

```python
class Config(BaseSettings):
    agents: AgentsConfig          # Provider, model, tokens
    services: ServicesConfig      # Heartbeat, monologue, cron, dream
    channels: ChannelsConfig      # Send progress, tool hints
    providers: list[CustomProvider]
    transcription: TranscriptionConfig
    gateway: GatewayConfig       # Host, port
    security: SecurityConfig     # Blocked patterns, ask_tools
    tools: ToolsConfig           # Web, exec, MCP
```

### Environment Variables

Config supports `${VAR}` interpolation from `.env` files:

```json
{
  "providers": [{
    "name": "provider_a",
    "apiKey": "${NVIDIA_API}",
    "apiBase": "${NVIDIA_BASE_URL}",
    "model": "nvidia_nim/stepfun-ai/step-3.5-flash"
  }]
}
```

## Key Algorithms

### Tool Call Block Detection

Messages are segmented into:

- **conv** - User + assistant conversation (no tool_calls)
- **tool** - Assistant with tool_calls + following tool results
- **skip** - Assistant responding to tool result (marked at creation)

`_is_skip` marker is set when assistant responds directly after tool results.

### Legal Tool Call Boundary

`_find_legal_start()` ensures every tool result has a matching assistant tool_call:

1. Track declared tool_call IDs
2. If orphan tool result found, start after it
3. Re-track remaining tool_call IDs

### Hard Clipping

When Step 1+2 don't free enough space:

1. Build combined timeline (messages + summary_lines)
2. Sort by timestamp (oldest first)
3. Clip from oldest until under target
4. Archive clipped to JSONL
5. Rebuild session

## Extension Points

### Adding a New Channel

1. Create `speckbot/channels/mychannel.py`
2. Subclass `BaseChannel`
3. Implement `start()`, `stop()`, `send()`, `is_allowed()`
4. Register in `speckbot/channels/registry.py`

### Adding a New Tool

1. Create `speckbot/agent/tools/mytool.py`
2. Subclass `Tool`
3. Implement `execute()`, `to_schema()`
4. Register in `AgentLoop._register_default_tools()`

### Adding a New Provider

1. Create `speckbot/providers/myspec.py`
2. Define provider spec in registry
3. Or use `provider_type: "litellm"` in config

## Glossary

- **Conveyor Belt** - Memory consolidation that archives old messages to JSONL
- **Tool Block** - Assistant tool_calls + following tool results
- **Skip Marker** - `_is_skip` flag marking assistant response after tool results
- **Summary Lines** - Summarized context displayed in system prompt
- **Archive** - JSONL files storing old messages
- **Ask Tools** - Tools requiring user confirmation
- **Block Detector** - Security detector for sensitive patterns
- **UnifiedTimer** - Single timer coordinating heartbeat/monologue/dream

## File Structure

```
speckbot/
├── agent/ (loop.py, context.py, security.py, subagent.py, definitions.py)
├── bus/ (events.py, queue.py, channels/)
├── cli/ (commands.py, templates/)
├── config/ (schema.py, loader.py, paths.py)
├── providers/ (base.py, litellm_provider.py, custom_provider.py, registry.py)
├── security/ (security.py, detectors/)
├── services/ (timer.py, monologue/, heartbeat/, cron/, dream/)
├── session/ (manager.py, memory.py)
├── skills/ (__init__.py [was agent/skills.py], README.md, skill-creator/)
├── tools/ (__init__.py, base.py, bash.py, cron.py, filesystem.py, mcp.py, message.py, registry.py, spawn.py, web.py)
├── utils/ (helpers.py, constants.py, evaluator.py)
├── __init__.py
├── __main__.py
├── README.md
└── pyproject.toml
```