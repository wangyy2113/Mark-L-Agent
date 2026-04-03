#!/usr/bin/env python3
"""Test card markdown normalization and optionally preview with live API.

Usage:
    # Unit tests only (no API needed):
    python tests/test_card_markdown.py

    # Live test: call Claude API and show normalized output:
    python tests/test_card_markdown.py --live "CA业务是干什么的"

    # Live test + send to Feishu chat for visual check:
    python tests/test_card_markdown.py --live "CA业务是干什么的" --chat oc_xxx
"""

import argparse
import json
import sys
import os

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.card import _normalize_markdown, _build_card, _build_streaming_card


# ── Unit tests ──

def test_heading_after_text():
    md = "一些内容\n### 标题\n更多内容"
    out = _normalize_markdown(md)
    assert "\n\n### 标题" in out, f"FAIL: heading not separated\n{out}"

def test_heading_already_spaced():
    md = "一些内容\n\n### 标题\n更多内容"
    out = _normalize_markdown(md)
    assert "\n\n\n" not in out, f"FAIL: triple newline introduced\n{out}"

def test_table_after_text():
    md = "描述文字\n| 列1 | 列2 |\n| --- | --- |\n| 数据 | 数据 |"
    out = _normalize_markdown(md)
    assert "\n\n| 列1" in out, f"FAIL: table not separated\n{out}"

def test_table_after_heading():
    md = "### 两大项目职责\n| 项目 | 定位 |\n| --- | --- |"
    out = _normalize_markdown(md)
    assert "\n\n| 项目" in out, f"FAIL: table after heading not separated\n{out}"

def test_table_rows_not_split():
    md = "| a | b |\n| --- | --- |\n| c | d |"
    out = _normalize_markdown(md)
    assert out == md, f"FAIL: table rows were split\n{out}"

def test_code_fence_after_text():
    md = "代码如下\n```java\nSystem.out.println();\n```"
    out = _normalize_markdown(md)
    assert "\n\n```java" in out, f"FAIL: code fence not separated\n{out}"

def test_blockquote_after_text():
    md = "总结\n> 引用内容"
    out = _normalize_markdown(md)
    assert "\n\n> 引用内容" in out, f"FAIL: blockquote not separated\n{out}"

def test_blockquote_continuation():
    md = "> 第一行\n> 第二行"
    out = _normalize_markdown(md)
    assert out == md, f"FAIL: blockquote continuation split\n{out}"

def test_complex_document():
    """Simulate a real LLM response with multiple block elements crammed together."""
    md = (
        "CA 系统概述\n"
        "## 系统做了什么？\n"
        "整个 CA 系统分为两大项目：\n"
        "```\n"
        "外部数据源 → CACS → CA-BOS\n"
        "```\n"
        "### 两大项目职责\n"
        "| 项目 | 定位 | 关键模块 |\n"
        "| --- | --- | --- |\n"
        "| corporate-action-source | 数据中心 | cacs-task |\n"
        "| corporate-action | 业务执行 | bos |\n"
        "### 四种执行模式\n"
        "| 模式 | 说明 |\n"
        "| --- | --- |\n"
        "| 分红自动执行 | 90%自动化 |\n"
        "> 注意：以上仅为简要概述"
    )
    out = _normalize_markdown(md)

    checks = [
        ("\n\n## 系统", "heading after text"),
        ("\n\n```\n", "code fence after text"),
        ("\n\n### 两大项目", "heading after code fence"),
        ("\n\n| 项目", "table after heading"),
        ("\n\n### 四种", "heading after table"),
        ("\n\n| 模式", "table after heading 2"),
        ("\n\n> 注意", "blockquote after table"),
    ]
    for pattern, label in checks:
        assert pattern in out, f"FAIL [{label}]: pattern not found\n---\n{out}"

def test_card_json_structure():
    """Verify the card JSON has correct v2 structure."""
    card_json = _build_card("### 测试\n内容")
    card = json.loads(card_json)
    assert card["schema"] == "2.0"
    assert "body" in card
    assert card["body"]["elements"][0]["tag"] == "markdown"


def run_unit_tests():
    tests = [v for k, v in globals().items() if k.startswith("test_")]
    passed = failed = 0
    for t in tests:
        try:
            t()
            print(f"  ✓ {t.__name__}")
            passed += 1
        except AssertionError as e:
            print(f"  ✗ {t.__name__}: {e}")
            failed += 1
    print(f"\n{passed} passed, {failed} failed")
    return failed == 0


# ── Live test ──

def live_test(question: str, chat_id: str = ""):
    """Call Claude API with the actual system prompt, show normalized output."""
    try:
        from anthropic import Anthropic
    except ImportError:
        print("ERROR: pip install anthropic  (needed for live test)")
        return

    from dotenv import load_dotenv
    load_dotenv()

    from agent import build_system_prompt, OUTPUT_STYLE_GUIDE
    from core.config import get_settings
    s = get_settings()

    client = Anthropic(
        api_key=s.anthropic_api_key,
        base_url=s.anthropic_base_url or None,
    )
    model = s.claude_model
    print(f"Calling {model} with question: {question}")
    print("=" * 60)

    response = client.messages.create(
        model=model,
        max_tokens=2000,
        system=build_system_prompt() + OUTPUT_STYLE_GUIDE,
        messages=[{"role": "user", "content": question}],
    )

    raw = response.content[0].text
    normalized = _normalize_markdown(raw)

    print("── Raw output ──")
    print(raw)
    print()
    print("── Normalized output ──")
    print(normalized)
    print()

    # Show diff
    if raw != normalized:
        print("── Changes ──")
        raw_lines = raw.split("\n")
        norm_lines = normalized.split("\n")
        import difflib
        diff = difflib.unified_diff(raw_lines, norm_lines, lineterm="", n=1)
        for line in diff:
            if line.startswith("+") and not line.startswith("+++"):
                print(f"  \033[32m{line}\033[0m")
            elif line.startswith("-") and not line.startswith("---"):
                print(f"  \033[31m{line}\033[0m")
    else:
        print("(no changes needed)")

    # Optionally send to Feishu
    if chat_id:
        print(f"\nSending to Feishu chat {chat_id}...")
        from core.card import send_message
        send_message(chat_id, normalized)
        print("Sent! Check Feishu to verify rendering.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Test card markdown normalization")
    parser.add_argument("--live", type=str, help="Run live test with this question")
    parser.add_argument("--chat", type=str, default="", help="Feishu chat_id to send test output to")
    args = parser.parse_args()

    print("Running unit tests...\n")
    ok = run_unit_tests()

    if args.live:
        print("\n" + "=" * 60)
        print("Running live test...\n")
        live_test(args.live, args.chat)

    sys.exit(0 if ok else 1)
