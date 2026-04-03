# CLAUDE.md

This file provides guidance to Claude Code when working with this repository.

## Project Overview

Mark-L Agent 是一个基于飞书的多 Agent 协作框架。通过飞书 WebSocket 接收消息，使用 Claude Agent SDK 驱动多 Agent 编排，集成飞书 MCP、GitLab MCP 等工具。

**Tech stack:** Python 3.12, Claude Agent SDK, lark-oapi (Feishu WebSocket), Pydantic Settings, SQLite WAL.

## Running

```bash
.venv/bin/python3.12 main.py
```

## Architecture

**Request flow:** Feishu WebSocket → `main.py` → `event_handler.py` (message routing) → `agent.py` (builds options, runs agent via `core/runner.py`) → `core/card.py` (streaming card responses).

### Key Directories

- **`core/`** — Infrastructure: runner, card engine, sessions, permissions, usage tracking, biz domain loading, MCP config, scheduler
- **`agents/`** — Agent definitions (AgentConfig dataclass + registry): dev, ask, ops, role
- **`tools/`** — Tool groups: base, dev, feishu, gitlab, ops, browser. `compose_tools()` merges groups
- **`biz/`** — Business domains: knowledge/ (knowledge base), context/ (business context), repos/ (code repos)
- **`skills/`** — Agent skill definitions (SKILL.md)
- **`mcp_servers/`** — Custom MCP servers (ops/prometheus)

### MCP Servers (mcp.json)

| Server | Type | Purpose |
|--------|------|---------|
| agent-feishu-mcp | HTTP (TAT) | Feishu doc read/write |
| agent-feishu-mcp-uat | HTTP (UAT) | Feishu search (personal identity) |
| agent-lark-mcp | stdio | Bitable, group chat, messages |

### Config Files

- `.env` — Feishu credentials, Claude API, admin ID
- `config.yaml` — Agent list, permission groups, budgets
- `mcp.json` — MCP server configuration
- `permissions.json` — User permission whitelist

## Key Environment Variables

`FEISHU_APP_ID`, `FEISHU_APP_SECRET`, `ANTHROPIC_API_KEY`, `ANTHROPIC_BASE_URL`, `ADMIN_OPEN_ID`, `CLAUDE_MODEL`, `BOT_NAME`.
