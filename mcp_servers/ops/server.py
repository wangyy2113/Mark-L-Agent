"""Ops MCP server — exposes operational tools via FastMCP.

Run: python -m mcp_servers.ops.server
"""

from mcp.server.fastmcp import FastMCP

from .prometheus import query_prometheus as _query_prometheus

mcp = FastMCP("ops-mcp")


@mcp.tool()
def query_prometheus(query: str, start: str = "", end: str = "", step: str = "") -> str:
    """查询 Prometheus 监控指标。

    支持 PromQL 表达式，支持 now-1h 等相对时间。
    不指定 start/end 时执行即时查询，指定时执行范围查询。

    Args:
        query: PromQL 表达式，如 up, rate(http_requests_total[5m])
        start: 范围查询开始时间，支持 now-1h / Unix 时间戳 / RFC3339
        end: 范围查询结束时间
        step: 查询步长，默认 1m
    """
    return _query_prometheus(query, start, end, step)


if __name__ == "__main__":
    mcp.run()
