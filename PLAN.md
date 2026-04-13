# SpeckBot Token Economy Overhaul Plan

> Concise. Modular. Compaction-ready.

---

## Context

**Problem**: SpeckBot uses 6000-7000 tokens at session start vs Pi's ~1000 tokens.
**Goal**: Reduce token usage while keeping functionality.

---

## Phase 1: Tool Refactoring (UNIX-based)

### 1.1 bash.py
- Rename `shell.py` → `bash.py`
- Rename tool: `exec` → `bash`
- Keep: security guards, timeout, restrict_to_workspace
- Add: Unix command hints in description (cat, sed, awk, etc.)

### 1.2 filesystem.py
| Tool | Description Update |
|------|-------------------|
| read_file | "Read file contents (cat/head/tail)" |
| write_file | "Write file (echo/tee)" |
| edit_file | "Edit file (sed/awk)" |
| list_dir | "List directory (ls)" |

### 1.3 loop.py
- Update tool registration to point to new `bash` module
- Update imports

**Files**: `bash.py`, `filesystem.py`, `loop.py`

---

## Phase 2: Workspace Cleanup

### Bootstrap Files
| File | Status | Reason |
|------|--------|--------|
| AGENTS.md | ✅ Keep | User instructions |
| MEMORY.md | ✅ Keep | Saved knowledge index |
| HEARTBEAT.md | Runtime | Not bootstrapped |
| SOUL.md | ❌ Remove | Redundant |
| USER.md | ❌ Remove | Redundant |
| HISTORY.md | ❌ Remove | Replaced by conveyor belt |
| JOURNAL.md | ❌ Remove | Redundant |

### Implementation
- Update `context.py`: BOOTSTRAP_FILES list
- Update `context.py`: _BOOTSTRAP_DESCRIPTIONS dict

**Files**: `context.py`

---

## Phase 3: Conveyor Belt System

### Concept
- Active window: 6000 tokens (configurable)
- Overflow: → archive.jsonl (per session, raw JSONL)
- Retrieval: User queries on-demand (manual read)

### Changes

| Component | Before | After |
|-----------|--------|-------|
| Trigger | >50% context window | Configurable threshold |
| Old messages | Summarized → HISTORY.md | Raw → archive.jsonl |
| Retrieval | Loaded in system | Manual query |
| Consolidation | LLM summary | Raw archive |

### Config Additions
```python
# schema.py - AgentsConfig
conveyor_window_tokens: int = 6000
conveyor_enabled: bool = True
```

### Files
- `manager.py` - Session archive handling
- `memory.py` - Archive logic (replace consolidate)
- `schema.py` - Config fields
- `constants.py` - Remove CONTEXT_PRESETS

---

## Phase 4: Context Engineering

### Identity Streamline (~200 tokens)
```
Before: ~500 tokens (runtime, workspace, monologue, platform, security, guidelines)
After:  ~200 tokens (runtime, workspace, platform only)
```

### Changes
- Simplify `_get_identity()` in context.py
- Remove monologue section (unless enabled)
- Remove security rules from identity
- Remove guidelines from identity

### Skills On-Demand
- Remove `always_skills` from system prompt
- Keep skill summary only (use /skill:name to load)

### Preset Removal
- Remove `context_level` from schema
- Remove CONTEXT_PRESETS from constants.py

**Files**: `context.py`, `schema.py`, `constants.py`

---

## Token Savings Estimate

| Component | Before | After | Savings |
|-----------|--------|-------|---------|
| Tools (descriptions) | ~4000 | ~3500 | ~500 |
| Bootstrap files | ~3000 | ~500 | ~2500 |
| Identity | ~500 | ~200 | ~300 |
| History | ~2000 | 0 (archive) | ~2000 |
| Skills | ~500 | 0 (on-demand) | ~500 |
| **TOTAL** | **~10000** | **~4200** | **~5800** |

---

## Execution Order

1. **Phase 1**: bash.py rename + filesystem descriptions
2. **Phase 2**: Workspace cleanup (bootstrap files)
3. **Phase 3**: Conveyor belt system
4. **Phase 4**: Context engineering

---

## Notes

- Each phase is modular - can be done independently
- Conveyor belt Phase 3 is the biggest change - consider doing last
- Keep backwards compatibility where possible
- Document changes for users