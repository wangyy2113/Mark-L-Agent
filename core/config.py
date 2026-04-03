"""Configuration loaded from environment variables / .env file."""

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # ── Feishu credentials ──
    feishu_app_id: str
    feishu_app_secret: str
    feishu_verification_token: str = ""
    feishu_encrypt_key: str = ""

    # ── Claude API ──
    anthropic_api_key: str
    anthropic_base_url: str = ""
    claude_model: str = "claude-opus-4-6"

    # ── Lite provider (OpenAI-compatible) ──
    llm_provider: str = "claude"            # "claude" | "lite"
    lite_api_key: str = ""                  # OpenAI-compatible API key
    lite_base_url: str = ""                 # e.g. "https://generativelanguage.googleapis.com/v1beta/openai/"
    lite_model: str = "gemini-2.0-flash"    # model identifier

    # ── MCP config ──
    mcp_config_path: str = "./mcp.json"

    # ── Permissions ──
    admin_open_id: str = ""  # Initial admin open_id, auto-added to whitelist on first start
    bot_open_id: str = ""    # Bot's own open_id, used to strip @bot mentions in group chats

    # ── Feishu doc defaults ──
    feishu_doc_folder_token: str = ""  # Default folder for create-doc (from drive URL)
    feishu_personal_token: str = ""    # Personal token for feishu-mcp UAT (mcp_xxx)
    feishu_project_token: str = ""     # Token for feishu-project MCP (m-xxx)

    # ── Dev mode ──
    biz_base_path: str = "./biz"  # relative to service dir, or absolute

    # ── Bot identity ──
    bot_name: str = ""        # Display name, e.g. "Zagi", "Z-One"
    bot_tagline: str = ""     # One-liner, e.g. "研发团队的 AI 协作伙伴"

    # ── Session ──
    session_ttl_seconds: int = 86400  # 24 hours
    session_db_path: str = "./data/sessions.db"

    model_config = {"env_file": (".env", ".env.bot"), "env_file_encoding": "utf-8", "extra": "ignore"}


@lru_cache
def get_settings() -> Settings:
    return Settings()
