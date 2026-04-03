"""Verify nested query() calls inside in-process MCP tool handlers.

This tests the core assumption of the orchestrator design:
a tool handler can call query() to run a sub-agent while the outer
query() waits for the tool result.

Usage:
    python tests/test_nested_sdk.py              # full nested test
    python tests/test_nested_sdk.py --inner-only  # test inner agent alone

Requires ANTHROPIC_API_KEY in .env or environment.
"""

import asyncio
import os
import sys
import traceback
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from claude_agent_sdk import (
    query,
    ClaudeAgentOptions,
    AssistantMessage,
    ResultMessage,
    tool as sdk_tool,
    create_sdk_mcp_server,
)


def _get_env() -> dict:
    """Get env dict with API key for SDK subprocess."""
    env = {}
    key = os.environ.get("ANTHROPIC_API_KEY", "")
    if key:
        env["ANTHROPIC_API_KEY"] = key
    base = os.environ.get("ANTHROPIC_BASE_URL", "")
    if base:
        env["ANTHROPIC_BASE_URL"] = base
    return env


# ── Inner agent: a simple agent that answers a math question ──

async def run_inner_agent(question: str) -> str:
    """Run a minimal agent that answers a question. No tools, just LLM."""
    print(f"  [inner] Starting inner agent with: {question}", flush=True)

    options = ClaudeAgentOptions(
        system_prompt="你是一个计算助手。直接回答数学问题，只输出答案数字，不要解释。",
        max_turns=1,
        max_budget_usd=0.01,
        permission_mode="bypassPermissions",
        env=_get_env(),
    )

    # Capture text from both AssistantMessage and ResultMessage
    # (ResultMessage.result may be empty for short responses —
    #  the text lives in AssistantMessage blocks instead)
    pending_text = ""
    result_text = ""
    async for message in query(prompt=question, options=options):
        if isinstance(message, AssistantMessage):
            for block in message.content:
                if hasattr(block, "text") and block.text:
                    pending_text = block.text.strip()
                    print(f"  [inner] Assistant: {pending_text[:100]}", flush=True)
        elif isinstance(message, ResultMessage):
            result_text = message.result or pending_text
            print(f"  [inner] Done. result={result_text[:100]}, cost=${message.total_cost_usd:.4f}", flush=True)

    return result_text


# ── Outer agent: has a tool that calls the inner agent ──

def build_delegate_server():
    """Create MCP server with a delegate tool that nests query()."""

    @sdk_tool(
        "ask_calculator",
        "向计算助手提问一个数学问题，返回计算结果",
        {"question": str},
    )
    async def ask_calculator(args):
        question = args.get("question", "")
        print(f"  [delegate] Handler called with: {question}", flush=True)

        try:
            answer = await run_inner_agent(question)
            print(f"  [delegate] Inner agent returned: {answer!r}", flush=True)
            return {
                "content": [{"type": "text", "text": f"计算结果: {answer}"}],
            }
        except Exception as e:
            tb = traceback.format_exc()
            print(f"  [delegate] Error:\n{tb}", flush=True)
            return {
                "content": [{"type": "text", "text": f"计算失败: {e}"}],
                "is_error": True,
            }

    return create_sdk_mcp_server(
        name="delegate",
        version="1.0.0",
        tools=[ask_calculator],
    )


async def test_inner_only():
    """Test that the inner agent works standalone."""
    print("Testing inner agent standalone...")
    result = await run_inner_agent("137 + 258 等于多少")
    print(f"Inner agent result: {result!r}")
    return result


async def test_nested():
    """Test the full nested orchestrator pattern."""
    delegate_server = build_delegate_server()

    options = ClaudeAgentOptions(
        system_prompt=(
            "你是一个助手。当用户问数学问题时，必须使用 ask_calculator 工具来计算，不要自己算。"
            "将工具返回的结果告诉用户。"
        ),
        mcp_servers={"delegate": delegate_server},
        allowed_tools=["mcp__delegate__ask_calculator"],
        max_turns=10,
        max_budget_usd=0.10,
        permission_mode="bypassPermissions",
        env=_get_env(),
    )

    print("[outer] Starting outer agent...", flush=True)
    result_text = ""
    turn = 0

    async for message in query(prompt="请帮我算一下 137 + 258 等于多少", options=options):
        if isinstance(message, AssistantMessage):
            turn += 1
            for block in message.content:
                if hasattr(block, "name"):
                    tool_input = getattr(block, "input", {}) or {}
                    print(f"[outer] Turn {turn}: tool call → {block.name}({tool_input})", flush=True)
                elif hasattr(block, "text") and block.text:
                    print(f"[outer] Turn {turn}: text → {block.text[:200]}", flush=True)
        elif isinstance(message, ResultMessage):
            result_text = message.result or ""
            print(f"[outer] Done. turns={message.num_turns}, cost=${message.total_cost_usd:.4f}", flush=True)

    return result_text


def main():
    # Load .env for API key
    try:
        from dotenv import load_dotenv
        load_dotenv(Path(__file__).parent.parent / ".env")
    except ImportError:
        pass

    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("Error: ANTHROPIC_API_KEY not set. Check .env or environment.")
        sys.exit(1)

    inner_only = "--inner-only" in sys.argv

    print("=" * 60)
    if inner_only:
        print("Testing inner agent only (no nesting)")
    else:
        print("Testing nested query() inside in-process MCP tool handler")
    print("=" * 60)

    if inner_only:
        result = asyncio.run(test_inner_only())
    else:
        result = asyncio.run(test_nested())

    print()
    print("=" * 60)
    print(f"Final result: {result!r}")
    print("=" * 60)

    if result:
        print("\n✅ Test passed!")
    else:
        print("\n❌ No result returned.")


if __name__ == "__main__":
    main()
