# SpeckBot Implementation Notes

## What's Been Done

This document tracks major architectural changes and features implemented in SpeckBot.

---

## Recent Changes (April 2026)

### Priority 1: Delete Dead Code ✅
**Issue:** Duplicate `_save_turn` method existed in both `AgentLoop` and `MessageHandler`, but `AgentLoop._save_turn` was never called.

**Fix:** Deleted the unused `AgentLoop._save_turn` method (~55 lines of dead code).

---

### Priority 2: Extract Shared Consolidation Function ✅
**Issue:** LLM consolidation logic (~65 lines) was duplicated in:
- `loop.py` (`_handle_flush` method)
- `timer.py` (`_trigger_restart` method)

**Fix:** Extracted to shared function in `memory.py`:
```python
async def consolidate_oldest_messages(
    session, provider, model, sessions, archive_dir
) -> str:
    """Consolidate oldest 90% of session messages via LLM."""
```
Both `/flush` command and Dream now use identical consolidation logic.

---

### Priority 3: Config Schema Cleanup ✅
**Issue:** `config/schema.py` had messy backward compatibility:
- `@property` shims (`tools.web`, `tools.exec`) returning constructed objects
- Deprecated alias classes at bottom of file
- Inconsistent naming (`ExecToolConfig` vs actual `bash` tool)

**Fix:** Clean refactor with no backward compat:
1. Removed `@property` shims from `ToolsConfig`
2. Removed deprecated classes (`ExecToolConfig`, `WebToolsConfig`, `MonologueConfig`, `HeartbeatConfig`)
3. Renamed `ExecToolConfig` → `BashToolConfig` (matches `bash` tool name)
4. Updated `cli/commands.py` to create config objects from flattened fields:
   ```python
   web_search_config=WebSearchConfig(
       provider=config.tools.web_search_provider,
       api_key=config.tools.web_search_api_key,
       base_url=config.tools.web_search_base_url,
       max_results=config.tools.web_search_max_results,
   ),
   exec_config=BashToolConfig(
       timeout=config.tools.exec_timeout,
       path_append=config.tools.exec_path_append,
       bash_path=config.tools.exec_bash_path,
   ),
   ```

---

### Priority 4: Dream Integration with Flush ✅
**Issue:** Dream's timer restart wasn't using LLM consolidation.

**Fix:**
1. `UnifiedTimer` now accepts `provider` and `model` params
2. Dream flush now runs LLM consolidation before restart
3. Both `/flush` and Dream use shared `consolidate_oldest_messages()` function

---

## Configuration System

### ToolsConfig (Flattened)
```
tools:
  web_proxy: str | None
  web_search_provider: str ("brave")
  web_search_api_key: str
  web_search_base_url: str
  web_search_max_results: int (5)
  exec_timeout: int (60)
  exec_path_append: str
  exec_bash_path: str | None
  restrict_to_workspace: bool
  mcp_servers: dict[MCPServerConfig]
```

### BashToolConfig (Object)
```python
class BashToolConfig(Base):
    timeout: int = 60
    path_append: str = ""
    bash_path: str | None = None
```

### WebSearchConfig (Object)
```python
class WebSearchConfig(Base):
    provider: str = "brave"
    api_key: str = ""
    base_url: str = ""
    max_results: int = 5
```

---

## Architecture Notes

### Agent Processing Flow
```
User Message → AgentLoop._dispatch() → AgentLoop._process_message()
  → MessageHandler.process() → MessageHandler._save_turn() → Session Save
```

### Memory Consolidation Flow
```
1. Build timeline from messages + summary_lines
2. Sort by timestamp (oldest first)
3. 90%/10% split
4. Archive oldest 90% to JSONL
5. LLM consolidation (summarize oldest messages)
6. Rebuild session (keep newest 10% + LLM summary)
7. Save session
```

---

## Code Statistics
- **Dead code removed:** ~55 lines (`AgentLoop._save_turn`)
- **Duplication eliminated:** ~130 lines (loop.py + timer.py consolidation)
- **Schema cleaned:** Removed ~60 lines of backward compat
- **Files affected:** `config/schema.py`, `agent/loop.py`, `agent/subagent.py`, `agent/memory.py`, `services/timer.py`, `cli/commands.py`

---

## Environment Variables

Config supports `${VAR}` interpolation from `.env` files:
```bash
# .env file
NVIDIA_API=your-key-here
NVIDIA_BASE_URL=https://api.nvidia.com/v1
OPENROUTER_API_1=your-key-here
OPENROUTER_BASE_URL=https://openrouter.ai/api/v1
```

Config example:
```json
{
  "providers": [
    {
      "name": "provider_a",
      "apiKey": "${NVIDIA_API}",
      "apiBase": "${NVIDIA_BASE_URL}",
      "model": "nvidia_nim/stepfun-ai/step-3.5-flash"
    }
  ]
}
```