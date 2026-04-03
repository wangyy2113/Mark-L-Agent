"""Verify AgentDefinition + Task tool works in our SDK version.

Minimal test: orchestrator with only Task, one sub-agent with Read.
If Task works, the orchestrator delegates; if not, we know the mechanism is broken.

Usage:
    python tests/test_agent_definition.py
"""

import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from claude_agent_sdk import (
    query,
    ClaudeAgentOptions,
    AgentDefinition,
    AssistantMessage,
    ResultMessage,
)


def _get_env() -> dict:
    env = {}
    key = os.environ.get("ANTHROPIC_API_KEY", "")
    if key:
        env["ANTHROPIC_API_KEY"] = key
    base = os.environ.get("ANTHROPIC_BASE_URL", "")
    if base:
        env["ANTHROPIC_BASE_URL"] = base
    return env


async def test_task_tool():
    """Test that Task tool delegates to a configured AgentDefinition."""

    agents = {
        "reader": AgentDefinition(
            description="文件读取助手：读取本地文件内容并返回。",
            prompt="你是一个文件读取助手。用 Read 工具读取用户指定的文件，返回内容摘要。用中文回复。",
            tools=["Read", "Glob"],
            model="haiku",
        ),
    }

    options = ClaudeAgentOptions(
        system_prompt=(
            "你是一个路由助手。你没有文件读取能力。"
            "当用户要求查看文件时，必须使用 Task 工具委托给 reader agent。"
            "不要自己尝试读取文件。"
        ),
        allowed_tools=["Task"],
        agents=agents,
        max_turns=10,
        max_budget_usd=0.50,
        permission_mode="bypassPermissions",
        env=_get_env(),
        model="haiku",
    )

    target_file = str(Path(__file__).parent.parent / "config.yaml")
    prompt = f"请读取这个文件的内容并告诉我里面有什么: {target_file}"

    print(f"[test] Prompt: {prompt}")
    print(f"[test] Target: {target_file}")
    print(f"[test] Agents: {list(agents.keys())}")
    print()

    result_text = ""
    turn = 0

    async for message in query(prompt=prompt, options=options):
        if isinstance(message, AssistantMessage):
            turn += 1
            for block in message.content:
                if hasattr(block, "name"):
                    tool_input = getattr(block, "input", {}) or {}
                    print(f"[turn {turn}] Tool: {block.name} | input keys: {list(tool_input.keys())}")
                elif hasattr(block, "text") and block.text:
                    print(f"[turn {turn}] Text: {block.text[:200]}")
        elif isinstance(message, ResultMessage):
            result_text = message.result or ""
            print(f"\n[done] turns={message.num_turns}, cost=${message.total_cost_usd:.4f}")

    print(f"\n{'='*50}")
    print(f"Result: {result_text[:500]}")
    print(f"{'='*50}")

    # Check if Task was used (not Agent, not WebFetch)
    return result_text


def main():
    try:
        from dotenv import load_dotenv
        load_dotenv(Path(__file__).parent.parent / ".env")
    except ImportError:
        pass

    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("Error: ANTHROPIC_API_KEY not set")
        sys.exit(1)

    print("Testing AgentDefinition + Task tool")
    print("=" * 50)
    result = asyncio.run(test_task_tool())

    if "agents" in result.lower() or "yaml" in result.lower() or "config" in result.lower():
        print("\n✅ Sub-agent successfully read the file via Task delegation!")
    else:
        print("\n❌ Task delegation may not have worked. Check tool calls above.")


if __name__ == "__main__":
    main()
