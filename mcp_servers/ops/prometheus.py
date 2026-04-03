"""Prometheus / Promxy query tool.

Supports instant query and range query with relative time expressions
like 'now-1h'. Ported from ops-assistant Go implementation.
"""

import json
import re
import time as _time

import requests

from .config import prometheus_config as _cfg


def _parse_prom_time(s: str) -> str:
    """Convert 'now', 'now-1h', RFC3339, or Unix timestamp to Unix timestamp string."""
    s = s.strip()
    if s == "now":
        return str(int(_time.time()))
    m = re.match(r"^now-(\d+)([smhdw])$", s)
    if m:
        val, unit = int(m.group(1)), m.group(2)
        multiplier = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}
        return str(int(_time.time()) - val * multiplier[unit])
    # Already a timestamp or RFC3339 — pass through
    return s


_MAX_RESULT_BYTES = 50_000
_MAX_SERIES = 50


def query_prometheus(query: str, start: str = "", end: str = "", step: str = "") -> str:
    """Execute a PromQL query against Prometheus / Promxy.

    Args:
        query: PromQL expression.
        start: Range start time — 'now-1h', Unix timestamp, or RFC3339. If both
               start and end are given, uses /api/v1/query_range.
        end: Range end time.
        step: Query resolution step (default '1m' for range queries).

    Returns:
        JSON string of query results, or an error message.
    """
    if not _cfg.url:
        return "错误：PROMETHEUS_URL 未配置"

    params = {"query": query}

    if start and end:
        url = f"{_cfg.url.rstrip('/')}/api/v1/query_range"
        params["start"] = _parse_prom_time(start)
        params["end"] = _parse_prom_time(end)
        params["step"] = step or "1m"
    else:
        url = f"{_cfg.url.rstrip('/')}/api/v1/query"

    auth = None
    if _cfg.username and _cfg.password:
        auth = (_cfg.username, _cfg.password)

    try:
        resp = requests.post(
            url,
            data=params,
            auth=auth,
            timeout=_cfg.timeout,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        resp.raise_for_status()
    except requests.RequestException as e:
        return f"Prometheus 请求失败: {e}"

    body = resp.json()
    if body.get("status") != "success":
        return f"Prometheus 错误: {body.get('error', 'unknown')}"

    result = body.get("data", {}).get("result", [])
    output = json.dumps(result, ensure_ascii=False)

    if len(output) > _MAX_RESULT_BYTES:
        limit = min(len(result), _MAX_SERIES)
        truncated = json.dumps(result[:limit], ensure_ascii=False)
        return (
            f"查询返回 {len(result)} 个时间序列，数据量过大（{len(output)} 字节），"
            f"仅展示前 {limit} 个：\n{truncated}"
        )

    return output
