"""Base tools: read-only + web access. Available to all roles.

Tool metadata follows Claude Code's pattern:
- readonly tools are safe for any permission level
- destructive tools require explicit permission
"""

# Read-only tools (safe for all users)
READONLY_TOOLS = ["Read", "Glob", "Grep"]

# Web access tools (read-only but external)
WEB_TOOLS = ["WebSearch", "WebFetch"]

# Combined base set
TOOLS = READONLY_TOOLS + WEB_TOOLS
