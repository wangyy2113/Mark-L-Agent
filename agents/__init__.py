"""Agent type definitions and registry.

Each agent type is an AgentConfig dataclass instance registered via register().
Chat mode is NOT an agent — it's the default path in the router.
"""

import logging
from dataclasses import dataclass, field
from typing import Callable, Any

logger = logging.getLogger(__name__)


@dataclass
class AgentConfig:
    """Agent type configuration. Define a new agent = fill a config."""

    # ── Identity ──
    name: str                           # "dev", "ops", "knowledge"
    display_name: str                   # "研发助手"
    description: str                    # shown in /agent list
    command: str                        # trigger command: "/dev", "/ops", "/ask"

    # ── Tools ──
    tools: list[str] = field(default_factory=list)
    tools_explore: list[str] | None = None      # explore mode tools; None = same as tools
    disallowed_explore: list[str] = field(default_factory=list)
    mcp_servers: dict = field(default_factory=dict)
    hooks: dict = field(default_factory=dict)

    # ── Domain ──
    requires_domain: bool = True        # needs biz/<domain> selection
    include_repos: bool = True          # load repos/ subdirectory
    include_claude_md: bool = True      # load CLAUDE.md from repos

    # ── Behavior ──
    max_turns: int = 50
    explore_max_turns: int = 20
    has_explore_mode: bool = True       # supports explore/requirement dual mode
    needs_isolation: bool = False       # per-chat worktree isolation

    # ── Model ──
    model: str = ""                     # per-agent default model (alias or full ID); empty = use global

    # ── Budget ──
    max_budget_usd: float = 0.0         # per-request cost cap; 0 = unlimited
    explore_max_budget_usd: float = 0.0 # explore mode cap (if different); 0 = use max_budget_usd

    # ── Session TTL ──
    session_ttl: int = 0                # per-agent session TTL in seconds; 0 = use global default

    # ── Phased workflow ──
    # Per-phase overrides: {"explore": {"tools": [...], "max_turns": 20, "budget": 1.0}, ...}
    phase_config: dict[str, dict] = field(default_factory=dict)

    # ── Prompt builder ──
    # Receives context dict, returns complete system prompt.
    # Context keys: domain_prompt, claude_md, requirement,
    #               roles_context, repos_path
    build_prompt: Callable[[dict[str, Any]], str] | None = None

    # ── Post-completion callback (optional) ──
    # Only dev agent needs this (git push detection).
    # Signature: (chat_id, result_text, context_dict) -> None
    on_complete: Callable[[str, str, dict], None] | None = None


# ── Registry ──

_registry: dict[str, AgentConfig] = {}


def register(config: AgentConfig) -> None:
    """Register an agent config. Called at module import time."""
    _registry[config.name] = config


def get_agent(name: str) -> AgentConfig | None:
    return _registry.get(name)


def list_agents() -> list[AgentConfig]:
    return list(_registry.values())


def find_by_command(cmd: str) -> AgentConfig | None:
    """Find agent by command prefix, e.g. '/dev' → dev agent."""
    for cfg in _registry.values():
        if cfg.command == cmd:
            return cfg
    return None


# ── Chat mode config (configurable via config.yaml) ──

@dataclass
class ChatConfig:
    max_budget_usd: float = 0.50
    max_turns: int = 30
    compress_threshold: int = 80_000  # input_tokens threshold for context compression
    compress_model: str = "claude-haiku-4-5"  # lightweight model for summary generation


_chat_config = ChatConfig()
_daily_budget_usd: float = 0.0
_default_mode: str = "chat"  # "chat" or "role"


def get_chat_config() -> ChatConfig:
    return _chat_config


def get_daily_budget_usd() -> float:
    return _daily_budget_usd


def get_default_mode() -> str:
    """Return the default mode: 'chat' or 'role' (orchestrator)."""
    return _default_mode


# Fields that config.yaml is allowed to override on AgentConfig
_OVERRIDABLE = {"max_budget_usd", "explore_max_budget_usd", "max_turns", "explore_max_turns", "model", "session_ttl"}

# Fields allowed inside phase_config overrides
_PHASE_OVERRIDABLE = {"budget", "max_turns"}


def apply_config_overrides(raw_config: dict) -> None:
    """Apply config.yaml overrides after all agent modules have registered.

    Parses optional sections: chat, agent_config, daily_budget_usd.
    Unknown keys are logged and skipped. Type mismatches are best-effort coerced.
    """
    global _chat_config, _daily_budget_usd, _default_mode

    if not raw_config or not isinstance(raw_config, dict):
        return

    # 0. default_mode
    mode = raw_config.get("default_mode")
    if mode and isinstance(mode, str) and mode in ("chat", "role"):
        _default_mode = mode
        logger.info("default_mode = %s", _default_mode)
    elif mode:
        logger.warning("default_mode: invalid value %r (expected 'chat' or 'role'), using default", mode)

    # 1. chat section → _chat_config
    chat_section = raw_config.get("chat")
    if chat_section and isinstance(chat_section, dict):
        for key, val in chat_section.items():
            if hasattr(_chat_config, key):
                expected_type = type(getattr(_chat_config, key))
                try:
                    setattr(_chat_config, key, expected_type(val))
                    logger.info("chat.%s = %s", key, getattr(_chat_config, key))
                except (ValueError, TypeError):
                    logger.warning("chat.%s: invalid value %r, skipped", key, val)
            else:
                logger.warning("chat.%s: unknown key, skipped", key)

    # 2. agent_config section → override registered AgentConfig fields
    agent_section = raw_config.get("agent_config")
    if agent_section and isinstance(agent_section, dict):
        for agent_name, overrides in agent_section.items():
            cfg = get_agent(agent_name)
            if not cfg:
                logger.warning("agent_config.%s: agent not registered, skipped", agent_name)
                continue
            if not isinstance(overrides, dict):
                continue

            for key, val in overrides.items():
                if key == "phase_config":
                    # Special handling: merge into existing phase_config
                    _apply_phase_overrides(cfg, val, agent_name)
                    continue
                if key not in _OVERRIDABLE:
                    logger.warning("agent_config.%s.%s: not overridable, skipped", agent_name, key)
                    continue
                expected_type = type(getattr(cfg, key))
                try:
                    setattr(cfg, key, expected_type(val))
                    logger.info("agent_config.%s.%s = %s", agent_name, key, getattr(cfg, key))
                except (ValueError, TypeError):
                    logger.warning("agent_config.%s.%s: invalid value %r, skipped", agent_name, key, val)

    # 3. daily_budget_usd
    daily = raw_config.get("daily_budget_usd")
    if daily is not None:
        try:
            _daily_budget_usd = float(daily)
            logger.info("daily_budget_usd = %s", _daily_budget_usd)
        except (ValueError, TypeError):
            logger.warning("daily_budget_usd: invalid value %r, skipped", daily)


def _apply_phase_overrides(cfg: AgentConfig, phase_overrides: dict, agent_name: str) -> None:
    """Merge phase_config overrides into an existing AgentConfig.phase_config.

    Only modifies budget/max_turns on phases that already exist.
    Does not create new phases or modify tools.
    """
    if not isinstance(phase_overrides, dict):
        return
    for phase_name, phase_vals in phase_overrides.items():
        if phase_name not in cfg.phase_config:
            logger.warning(
                "agent_config.%s.phase_config.%s: phase not defined, skipped",
                agent_name, phase_name,
            )
            continue
        if not isinstance(phase_vals, dict):
            continue
        existing = cfg.phase_config[phase_name]
        for key, val in phase_vals.items():
            if key not in _PHASE_OVERRIDABLE:
                logger.warning(
                    "agent_config.%s.phase_config.%s.%s: not overridable, skipped",
                    agent_name, phase_name, key,
                )
                continue
            try:
                existing[key] = float(val) if key == "budget" else int(val)
                logger.info(
                    "agent_config.%s.phase_config.%s.%s = %s",
                    agent_name, phase_name, key, existing[key],
                )
            except (ValueError, TypeError):
                logger.warning(
                    "agent_config.%s.phase_config.%s.%s: invalid value %r, skipped",
                    agent_name, phase_name, key, val,
                )
