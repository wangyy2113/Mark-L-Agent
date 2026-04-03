"""Ops MCP server configuration — loaded from environment variables.

Env var names align with the ops-assistant Go project conventions.
"""

import os


class PrometheusConfig:
    url: str = ""
    username: str = ""
    password: str = ""
    timeout: int = 30

    def __init__(self):
        self.url = os.environ.get("PROMXY_URL", "")
        self.username = os.environ.get("PROMXY_USERNAME", "")
        self.password = os.environ.get("PROMXY_PASSWORD", "")
        self.timeout = int(os.environ.get("PROMXY_TIMEOUT", "30"))


prometheus_config = PrometheusConfig()
