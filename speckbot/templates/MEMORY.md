# Memory Index

This is SpeckBot's memory index - an overview of all stored information. It is maintained by Dream (automatic memory cleanup that runs on every startup).

## What This File Contains

- **Knowledges**: Factual/technical knowledge you've saved
- **Projects**: Project-specific context and notes
- **Recent History**: Summaries of past conversations

## How Memory Works

- **Saving**: Use `save_knowledge` tool for facts, `save_project` tool for project info
- **Listing**: Use `list_memories` to see all saved memories
- **Reading**: Use `read_file` tool to read specific knowledge/project files
- **Dream Cleanup**: On every startup, Dream automatically:
  - Removes duplicate entries
  - Converts relative dates to absolute (e.g., "last week" → "2026-04-03")
  - Keeps HISTORY.md trimmed to max lines
  - Updates this MEMORY.md index

## Current Status

## Knowledges

(No knowledges saved yet. Use `save_knowledge` tool to save information.)

## Projects

(No projects saved yet. Use `save_project` tool to save project information.)

## Recent History

(No history entries yet. Conversations are consolidated when context gets full.)