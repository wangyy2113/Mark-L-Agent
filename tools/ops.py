"""Ops tools: custom Prometheus MCP + Alibaba Cloud Observability MCP.

Two MCP servers:
- ops-mcp: Custom FastMCP server for self-hosted Prometheus/Promxy.
- observability-mcp: Official alibabacloud-observability-mcp-server
  providing SLS log query, ARMS tracing, CMS metrics. Binary download:
  https://github.com/aliyun/alibabacloud-observability-mcp-server/releases
"""

import os
import sys
from pathlib import Path

# ── Custom ops-mcp (Prometheus) ──

OPS_MCP_TOOLS = [
    "mcp__ops-mcp__query_prometheus",
]

OPS_MCP_SERVERS: dict = {
    "ops-mcp": {
        "command": sys.executable,
        "args": ["-m", "mcp_servers.ops.server"],
        "cwd": str(Path(__file__).resolve().parent.parent),
    },
}

# ── Official Alibaba Cloud Observability MCP ──
# Install: download binary from GitHub releases, place in PATH or specify full path.
# Required env: ALIBABA_CLOUD_ACCESS_KEY_ID, ALIBABA_CLOUD_ACCESS_KEY_SECRET

OBSERVABILITY_TOOLS = [
    # SLS
    "mcp__observability-mcp__sls_list_projects",
    "mcp__observability-mcp__sls_list_logstores",
    "mcp__observability-mcp__sls_execute_sql",
    "mcp__observability-mcp__sls_text_to_sql",
    "mcp__observability-mcp__sls_get_context_logs",
    "mcp__observability-mcp__sls_log_explore",
    "mcp__observability-mcp__sls_log_compare",
    # CMS (PromQL generation — useful alongside custom Prometheus)
    "mcp__observability-mcp__cms_text_to_promql",
]

OBSERVABILITY_MCP_SERVERS: dict = {
    "observability-mcp": {
        "command": "alibabacloud-observability-mcp-server",
        "args": ["--transport", "stdio"],
        # Uses SLS_ACCESS_KEY_ID / SLS_ACCESS_KEY_SECRET from ops-assistant convention.
        # The official binary expects ALIBABA_CLOUD_ACCESS_KEY_* — mapped here.
        "env": {
            "ALIBABA_CLOUD_ACCESS_KEY_ID": os.environ.get("SLS_ACCESS_KEY_ID", ""),
            "ALIBABA_CLOUD_ACCESS_KEY_SECRET": os.environ.get("SLS_ACCESS_KEY_SECRET", ""),
        },
    },
}

# ── Exports ──

TOOLS: list[str] = OPS_MCP_TOOLS + OBSERVABILITY_TOOLS

MCP_SERVERS: dict = {**OPS_MCP_SERVERS, **OBSERVABILITY_MCP_SERVERS}
