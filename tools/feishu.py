"""Feishu MCP tool lists, separated into read and write groups.

Two feishu-mcp modes:
- agent-feishu-mcp: TAT (tenant access token) mode via developer endpoint.
  App identity, never expires. Available to all users.
- agent-feishu-mcp-uat: UAT (user access token) mode via personal OAuth.
  Personal identity, 7-day expiry. Admin-only (supports search-doc, search-user).
"""

# ── feishu-mcp TAT mode (app identity, all users) ──
# TAT does not support search-doc or search-user

FEISHU_READ = [
    "mcp__agent-feishu-mcp__fetch-doc",
    "mcp__agent-feishu-mcp__list-docs",
    "mcp__agent-feishu-mcp__get-comments",
    "mcp__agent-feishu-mcp__get-user",
    "mcp__agent-feishu-mcp__fetch-file",
]

FEISHU_WRITE = [
    "mcp__agent-feishu-mcp__create-doc",
    "mcp__agent-feishu-mcp__update-doc",
    "mcp__agent-feishu-mcp__add-comments",
    # TODO: 开通 drive:permission:member:create 权限后启用，TAT 创建的文档需要自动授权
    # "mcp__agent-lark-mcp__drive_v1_permissionMember_create",
]

# ── feishu-mcp UAT mode (personal identity, admin-only) ──

FEISHU_UAT = ["mcp__agent-feishu-mcp-uat__*"]

# ── lark-mcp (stdio MCP) ──

LARK_READ = [
    "mcp__agent-lark-mcp__bitable_v1_appTable_list",
    "mcp__agent-lark-mcp__bitable_v1_appTableField_list",
    "mcp__agent-lark-mcp__bitable_v1_appTableRecord_search",
    "mcp__agent-lark-mcp__contact_v3_user_batchGetId",
    "mcp__agent-lark-mcp__docx_v1_document_rawContent",
    "mcp__agent-lark-mcp__im_v1_chat_list",
    "mcp__agent-lark-mcp__im_v1_chatMembers_get",
    "mcp__agent-lark-mcp__im_v1_message_list",
    "mcp__agent-lark-mcp__wiki_v1_node_search",
    "mcp__agent-lark-mcp__wiki_v2_space_getNode",
    "mcp__agent-lark-mcp__docx_builtin_search",
]

LARK_WRITE = [
    "mcp__agent-lark-mcp__bitable_v1_app_create",
    "mcp__agent-lark-mcp__bitable_v1_appTable_create",
    "mcp__agent-lark-mcp__bitable_v1_appTableRecord_create",
    "mcp__agent-lark-mcp__bitable_v1_appTableRecord_update",
    "mcp__agent-lark-mcp__drive_v1_permissionMember_create",
    "mcp__agent-lark-mcp__im_v1_chat_create",
    "mcp__agent-lark-mcp__im_v1_message_create",
    # docx_builtin_import 需要 docs:document:import scope（未开通），
    # 文档创建用 feishu-mcp 的 create-doc 代替
]

# ── Wildcard (for admin full access) ──

FEISHU_ALL = ["mcp__agent-feishu-mcp__*"]
LARK_ALL = ["mcp__agent-lark-mcp__*"]
