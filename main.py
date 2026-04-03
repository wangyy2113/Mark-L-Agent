"""Entry point: Feishu WebSocket long-connection mode."""

import importlib
import logging
import logging.handlers
import os
import signal
import sys
import threading
import time

_start_time = time.monotonic()
_start_epoch = time.time()

try:
    from setproctitle import setproctitle
    setproctitle("mark-l-agent")
except ImportError:
    pass
from pathlib import Path

import lark_oapi as lark
from lark_oapi.api.im.v1 import P2ImMessageReceiveV1
from lark_oapi.event.callback.model.p2_card_action_trigger import (
    P2CardActionTrigger,
    P2CardActionTriggerResponse,
)

import agent
import core.audit as audit
import event_handler
from core.config import get_settings
from core.persistent_session import PersistentSessionStore, PersistentAgentSessionStore
from core.usage import UsageStore
from core.permissions import Permissions

_start_time = time.time()

_log_level = os.environ.get("LOG_LEVEL", "INFO").upper()
_log_format = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"

# Console handler (stdout, for systemd journald / foreground debugging)
logging.basicConfig(
    level=getattr(logging, _log_level, logging.INFO),
    format=_log_format,
)

# File handler (daily rotation, 30-day retention)
os.makedirs("data", exist_ok=True)
_file_handler = logging.handlers.TimedRotatingFileHandler(
    "data/app.log", when="D", backupCount=30, encoding="utf-8",
)
_file_handler.setFormatter(logging.Formatter(_log_format))
_file_handler.setLevel(getattr(logging, _log_level, logging.INFO))
logging.getLogger().addHandler(_file_handler)

logging.getLogger("claude_agent_sdk").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)


def get_start_time() -> float:
    """Return the process start timestamp (epoch seconds)."""
    return _start_epoch


def _checkpoint(label: str) -> None:
    """Log a startup timing checkpoint."""
    elapsed = (time.monotonic() - _start_time) * 1000
    logger.info("[Startup] %s: %.0fms", label, elapsed)


def _apply_logging_config(raw_config: dict) -> None:
    """Apply per-module log levels from config.yaml logging section.

    Example config.yaml:
        logging:
          level: INFO              # global default (overrides LOG_LEVEL env)
          loggers:
            agent: DEBUG
            core.biz: DEBUG
            core.runner: INFO
            core.card: WARNING

    The root handler level is lowered to the minimum of all configured levels,
    so that per-logger DEBUG can actually output even when the global level is INFO.
    Individual loggers above the minimum will filter their own messages.
    """
    section = raw_config.get("logging") if raw_config else None
    if not section or not isinstance(section, dict):
        return

    root = logging.getLogger()

    # Global level override
    global_level = section.get("level")
    if global_level and isinstance(global_level, str):
        level = getattr(logging, global_level.upper(), None)
        if level is not None:
            root.setLevel(level)
            logger.info("Global log level: %s", global_level.upper())

    # Per-logger levels
    min_level = root.level  # track lowest configured level
    loggers = section.get("loggers")
    if loggers and isinstance(loggers, dict):
        for name, level_str in loggers.items():
            if not isinstance(level_str, str):
                continue
            level = getattr(logging, level_str.upper(), None)
            if level is not None:
                logging.getLogger(name).setLevel(level)
                if level < min_level:
                    min_level = level
                logger.info("Logger %s: %s", name, level_str.upper())
            else:
                logger.warning("logging.loggers.%s: invalid level %r", name, level_str)

    # Lower root handler level so per-logger DEBUG messages can pass through.
    # Without this, root handler at INFO blocks DEBUG even if the logger accepts it.
    if min_level < root.level:
        for handler in root.handlers:
            handler.setLevel(min_level)
        # Set root logger level high so unconfigured loggers stay quiet
        # (per-logger levels override for configured ones)
        logger.info("Root handler level lowered to %s for per-logger config", logging.getLevelName(min_level))

# Graceful shutdown flag — checked by event_handler to reject new requests
shutting_down = threading.Event()
_SHUTDOWN_TIMEOUT = 30  # seconds to wait for active requests before exit


def _load_agents(config_path: str = "config.yaml") -> tuple[list[str], dict]:
    """Load agent modules listed in config.yaml.

    Each entry triggers importlib.import_module("agents.<name>"),
    which executes the module-level register() call.
    Returns (loaded_names, raw_config_dict).
    """
    p = Path(config_path)
    if not p.is_absolute():
        p = Path(__file__).parent / p
    if not p.exists():
        logger.info("No config.yaml found at %s, starting with chat mode only", p)
        return [], {}

    try:
        import yaml
        data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except ImportError:
        # Fallback: simple line-based parsing for the agents list
        logger.warning("PyYAML not installed, falling back to simple config parsing")
        data = _parse_simple_yaml(p)
    except Exception:
        logger.exception("Failed to read config.yaml from %s", p)
        return [], {}

    agent_names = data.get("agents", [])
    if not agent_names:
        logger.info("No agents listed in config.yaml, starting with chat mode only")
        return [], data

    loaded = []
    for name in agent_names:
        try:
            importlib.import_module(f"agents.{name}")
            loaded.append(name)
            logger.info("Loaded agent: %s", name)
        except Exception:
            logger.exception("Failed to load agent module: agents.%s", name)

    return loaded, data


def _parse_simple_yaml(p: Path) -> dict:
    """Minimal YAML parser for just the agents list (fallback when PyYAML missing)."""
    agents = []
    in_agents = False
    for line in p.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped.startswith("#") or not stripped:
            continue
        if stripped == "agents:" or stripped.startswith("agents:"):
            in_agents = True
            continue
        if in_agents:
            if stripped.startswith("- "):
                name = stripped[2:].strip().strip('"').strip("'")
                if name and not name.startswith("#"):
                    agents.append(name)
            elif not stripped.startswith("-"):
                in_agents = False
    return {"agents": agents}


def _apply_permission_groups(raw_config: dict, permissions: Permissions) -> None:
    """Load permission_groups from config.yaml and apply to Permissions instance."""
    from core.permissions import GroupConfig, DEFAULT_GROUPS

    section = raw_config.get("permission_groups") if raw_config else None
    if not section or not isinstance(section, dict):
        logger.info("No permission_groups in config.yaml, using defaults")
        return

    groups: dict[str, GroupConfig] = {}
    for name, cfg in section.items():
        if not isinstance(cfg, dict):
            continue
        groups[name] = GroupConfig(
            name=name,
            agents=cfg.get("agents", []),
            tools=cfg.get("tools", []),
            paths=cfg.get("paths", []),
        )

    # Ensure admin group always exists
    if "admin" not in groups:
        groups["admin"] = DEFAULT_GROUPS["admin"]

    permissions.set_group_configs(groups)
    logger.info("Loaded %d permission groups: %s", len(groups), list(groups.keys()))


def _on_message(data: P2ImMessageReceiveV1) -> None:
    """Callback from Feishu SDK — runs handler in a thread to avoid blocking WebSocket."""
    t = threading.Thread(target=_handle_safe, args=(data,), daemon=True, name="msg-handler")
    t.start()


def _handle_safe(data: P2ImMessageReceiveV1) -> None:
    try:
        event_handler.handle_message_event(data)
    except Exception:
        logger.exception("Unhandled error in message handler")


def _on_card_action(data: P2CardActionTrigger) -> P2CardActionTriggerResponse | None:
    """Callback from Feishu SDK — card button click. Synchronous (returns response)."""
    try:
        return event_handler.handle_card_action(data)
    except Exception:
        logger.exception("Unhandled error in card action handler")
        return None


def main() -> None:
    s = get_settings()
    _checkpoint("settings loaded")

    # Load agents from config.yaml (triggers register() calls)
    loaded_agents, raw_config = _load_agents()
    if raw_config:
        _apply_logging_config(raw_config)
        from agents import apply_config_overrides
        apply_config_overrides(raw_config)
    _checkpoint("agents loaded")
    if loaded_agents:
        from agents import list_agents
        logger.info("Registered agents: %s", [a.name for a in list_agents()])
    else:
        logger.info("No agents loaded, running in chat-only mode")

    # Shared database connection (SQLite WAL)
    import core.db
    core.db.init(s.session_db_path)

    # Session stores (use shared db connection)
    session_store = PersistentSessionStore(ttl=s.session_ttl_seconds, db_path=s.session_db_path)
    agent_session_store = PersistentAgentSessionStore(ttl=s.session_ttl_seconds, db_path=s.session_db_path)
    usage_store = UsageStore(db_path=s.session_db_path)

    # Permissions & audit
    permissions = Permissions()
    permissions.seed_admin(s.admin_open_id)
    _apply_permission_groups(raw_config, permissions)
    audit.init()

    # Initialize modules
    agent.init(session_store, agent_session_store, permissions, usage_store)
    event_handler.init(session_store, agent_session_store, permissions, usage_store)

    # Load skills (bundled + disk-based SKILL.md files)
    from core.skills import load_skills
    load_skills()

    _checkpoint("modules initialized")

    # Periodic worktree cleanup (daemon thread, runs hourly)
    def _worktree_cleanup_loop():
        while not shutting_down.is_set():
            shutting_down.wait(3600)
            if shutting_down.is_set():
                break
            try:
                from core.worktree import cleanup_stale_worktrees
                from core.biz import discover_domains
                for domain in discover_domains():
                    removed = cleanup_stale_worktrees(domain, max_age_hours=24)
                    if removed:
                        logger.info("Cleaned %d stale worktree(s) for domain=%s", removed, domain)
            except Exception:
                logger.exception("Error in worktree cleanup")

    threading.Thread(target=_worktree_cleanup_loop, daemon=True, name="worktree-cleanup").start()

    # Built-in scheduled tasks (P2 alert summary, error log summary at 11:00 and 16:00)
    try:
        from core.scheduler import start as start_scheduler
        start_scheduler(stop_event=shutting_down)
        logger.info("Scheduled task runner started")
    except Exception:
        logger.exception("Failed to start scheduled task runner")

    def _shutdown(signum, _frame):
        name = signal.Signals(signum).name
        logger.info("Received %s, shutting down...", name)
        shutting_down.set()
        # Wait for active request threads to finish
        deadline = time.time() + _SHUTDOWN_TIMEOUT
        active = [t for t in threading.enumerate() if t.name.startswith("msg-")]
        if active:
            logger.info("Waiting for %d active request(s)...", len(active))
        for t in active:
            remaining = deadline - time.time()
            if remaining > 0:
                t.join(timeout=remaining)
        logger.info("Shutdown complete")
        os._exit(0)  # Force exit — lark-oapi WebSocket threads don't respond to sys.exit

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGHUP, _shutdown)

    logger.info("Starting Feishu WebSocket client...")
    _checkpoint("pre-websocket")

    handler = (
        lark.EventDispatcherHandler.builder(
            s.feishu_verification_token,
            s.feishu_encrypt_key,
        )
        .register_p2_im_message_receive_v1(_on_message)
        .register_p2_card_action_trigger(_on_card_action)
        .register_p2_im_message_message_read_v1(lambda data: None)
        .register_p2_im_message_reaction_created_v1(lambda data: None)
        .register_p2_im_message_reaction_deleted_v1(lambda data: None)
        .build()
    )

    ws_client = lark.ws.Client(
        app_id=s.feishu_app_id,
        app_secret=s.feishu_app_secret,
        event_handler=handler,
        log_level=lark.LogLevel.INFO,
    )

    ws_client.start()

    # ── Warmup: pre-connect MCP servers in background ──
    # The first real request triggers Claude SDK subprocess + MCP connection
    # which takes 3-5s. Pre-fetching TAT token warms the lark-oapi token cache.
    def _warmup():
        try:
            t0 = time.monotonic()
            # 1. Warm up Feishu TAT token cache (used by feishu-mcp)
            from core.mcp import _get_tenant_access_token
            _get_tenant_access_token()
            # 2. Pre-import heavy modules that are lazy-loaded on first request
            import core.runner  # noqa: F401
            import core.card  # noqa: F401
            elapsed = (time.monotonic() - t0) * 1000
            logger.info("[Warmup] Done in %.0fms (TAT token + module preload)", elapsed)
        except Exception:
            logger.debug("[Warmup] Failed (non-critical)", exc_info=True)

    threading.Thread(target=_warmup, daemon=True, name="warmup").start()
    _checkpoint("websocket connected, warmup started")


if __name__ == "__main__":
    main()
