"""Browser MCP tool lists: Playwright and Chrome DevTools.

Playwright: browser automation — navigate, click, type, screenshot, snapshot.
Chrome DevTools: lower-level debugging — DOM inspection, JS eval, network,
  performance traces, Lighthouse audits.
"""

# ── Playwright MCP ──

PLAYWRIGHT_TOOLS = [
    "mcp__playwright__browser_navigate",
    "mcp__playwright__browser_navigate_back",
    "mcp__playwright__browser_click",
    "mcp__playwright__browser_hover",
    "mcp__playwright__browser_type",
    "mcp__playwright__browser_select_option",
    "mcp__playwright__browser_fill_form",
    "mcp__playwright__browser_press_key",
    "mcp__playwright__browser_snapshot",
    "mcp__playwright__browser_take_screenshot",
    "mcp__playwright__browser_drag",
    "mcp__playwright__browser_evaluate",
    "mcp__playwright__browser_run_code",
    "mcp__playwright__browser_file_upload",
    "mcp__playwright__browser_handle_dialog",
    "mcp__playwright__browser_tabs",
    "mcp__playwright__browser_close",
    "mcp__playwright__browser_resize",
    "mcp__playwright__browser_console_messages",
    "mcp__playwright__browser_network_requests",
    "mcp__playwright__browser_wait_for",
    "mcp__playwright__browser_install",
]

# ── Chrome DevTools MCP ──

CHROME_DEVTOOLS_TOOLS = [
    "mcp__chrome-devtools__navigate_page",
    "mcp__chrome-devtools__new_page",
    "mcp__chrome-devtools__list_pages",
    "mcp__chrome-devtools__select_page",
    "mcp__chrome-devtools__close_page",
    "mcp__chrome-devtools__click",
    "mcp__chrome-devtools__hover",
    "mcp__chrome-devtools__fill",
    "mcp__chrome-devtools__fill_form",
    "mcp__chrome-devtools__press_key",
    "mcp__chrome-devtools__type_text",
    "mcp__chrome-devtools__drag",
    "mcp__chrome-devtools__upload_file",
    "mcp__chrome-devtools__handle_dialog",
    "mcp__chrome-devtools__wait_for",
    "mcp__chrome-devtools__take_snapshot",
    "mcp__chrome-devtools__take_screenshot",
    "mcp__chrome-devtools__evaluate_script",
    "mcp__chrome-devtools__resize_page",
    "mcp__chrome-devtools__emulate",
    "mcp__chrome-devtools__list_console_messages",
    "mcp__chrome-devtools__get_console_message",
    "mcp__chrome-devtools__list_network_requests",
    "mcp__chrome-devtools__get_network_request",
    "mcp__chrome-devtools__performance_start_trace",
    "mcp__chrome-devtools__performance_stop_trace",
    "mcp__chrome-devtools__performance_analyze_insight",
    "mcp__chrome-devtools__lighthouse_audit",
    "mcp__chrome-devtools__take_memory_snapshot",
]

# ── Combined ──

BROWSER_TOOLS = PLAYWRIGHT_TOOLS + CHROME_DEVTOOLS_TOOLS

# ── Wildcards (for admin full access) ──

PLAYWRIGHT_ALL = ["mcp__playwright__*"]
CHROME_DEVTOOLS_ALL = ["mcp__chrome-devtools__*"]
BROWSER_ALL = PLAYWRIGHT_ALL + CHROME_DEVTOOLS_ALL
