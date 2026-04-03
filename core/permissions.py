"""Permission management: group-based access control with JSON persistence.

Groups define three dimensions of access:
- agents: which agent modes are available (chat, dev, ask, ops)
- tools: which tool sets for chat mode (base, dev, feishu_read, etc.)
- paths: allowed file path patterns (biz/*/repos, etc.)

Data is stored in permissions.json:
- admins: list of open_ids (always = "admin" group)
- groups: {group_name: [open_ids]} for non-admin groups
- chats: {chat_id: default_group_name}
- roles: {chat_id: {open_id: {name, desc}}} (prompt injection, not permission)
"""

import json
import logging
import threading
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass
class GroupConfig:
    """Permission group definition. Loaded from config.yaml."""

    name: str
    agents: list[str] = field(default_factory=list)   # e.g. ["chat", "dev", "ask"]
    tools: list[str] = field(default_factory=list)     # e.g. ["base", "dev", "feishu_read"]
    paths: list[str] = field(default_factory=list)     # e.g. ["biz/*/repos"]


# Default group configs (used when config.yaml doesn't define permission_groups)
DEFAULT_GROUPS: dict[str, GroupConfig] = {
    "admin": GroupConfig(
        name="admin",
        agents=["chat", "dev", "ask", "ops"],
        tools=["all"],
        paths=[],
    ),
    "developer": GroupConfig(
        name="developer",
        agents=["chat", "dev", "ask"],
        tools=["base", "dev", "feishu_read", "feishu_write", "lark_read"],
        paths=["*"],
    ),
    "member": GroupConfig(
        name="member",
        agents=["chat", "ask"],
        tools=["base", "feishu_read", "lark_read"],
        paths=["*"],
    ),
}


class Permissions:
    def __init__(self, path: str = "permissions.json"):
        self._path = Path(path)
        self._lock = threading.Lock()
        self._seeded_from_file = False
        self._data: dict = {
            "admins": [],
            "groups": {},       # {group_name: [open_id, ...]}
            "chats": {},        # {chat_id: group_name}
            "roles": {},        # {chat_id: {open_id: {"name": str, "desc": str}}}
        }
        self._group_configs: dict[str, GroupConfig] = dict(DEFAULT_GROUPS)
        self._sudo_overrides: dict[str, str] = {}  # {chat_id: group_name}
        self._load()

    # ── Persistence ──

    def _load(self) -> None:
        if not self._path.exists():
            return
        self._seeded_from_file = True
        try:
            raw = json.loads(self._path.read_text())

            if "admins" in raw and isinstance(raw["admins"], list):
                self._data["admins"] = raw["admins"]
            if "groups" in raw and isinstance(raw["groups"], dict):
                self._data["groups"] = raw["groups"]
            if "chats" in raw and isinstance(raw["chats"], dict):
                self._data["chats"] = raw["chats"]
            if "roles" in raw and isinstance(raw["roles"], dict):
                self._data["roles"] = raw["roles"]

            total_members = sum(len(v) for v in self._data["groups"].values())
            logger.info(
                "Loaded permissions: %d admins, %d group members, %d chats, %d groups with roles",
                len(self._data["admins"]), total_members,
                len(self._data["chats"]), len(self._data["roles"]),
            )
        except Exception:
            logger.exception("Failed to load permissions from %s", self._path)

    def _save(self) -> None:
        try:
            self._path.write_text(json.dumps(self._data, indent=2, ensure_ascii=False))
        except Exception:
            logger.exception("Failed to save permissions to %s", self._path)

    # ── Group config management ──

    def set_group_configs(self, configs: dict[str, GroupConfig]) -> None:
        """Set group configurations (called at startup from config.yaml)."""
        with self._lock:
            self._group_configs = configs

    def get_group_config(self, group_name: str) -> GroupConfig | None:
        """Get the configuration for a specific group."""
        with self._lock:
            return self._group_configs.get(group_name)

    def list_group_configs(self) -> dict[str, GroupConfig]:
        """List all group configurations."""
        with self._lock:
            return dict(self._group_configs)

    # ── Seed initial admin ──

    def seed_admin(self, open_id: str) -> None:
        """Add initial admin on first run only (permissions.json didn't exist yet)."""
        if not open_id:
            return
        with self._lock:
            if self._seeded_from_file:
                return
            if open_id not in self._data["admins"]:
                self._data["admins"].append(open_id)
                self._save()
                logger.info("Seeded initial admin: %s", open_id)

    # ── Core query methods ──

    def get_group(self, sender_id: str, chat_id: str = "") -> str | None:
        """Determine the permission group for a sender in a chat.

        Resolution order:
        1. Sudo override (admin testing) — only if sender is an actual admin
        2. Admin list → "admin"
        3. Named groups → group name
        4. Chat default group
        5. None (not authorized)
        """
        with self._lock:
            # Sudo override (only for actual admins doing testing)
            if chat_id and chat_id in self._sudo_overrides:
                if sender_id in self._data["admins"]:
                    return self._sudo_overrides[chat_id]

            # Admin check
            if sender_id in self._data["admins"]:
                return "admin"

            # Named groups
            for group_name, members in self._data["groups"].items():
                if sender_id in members:
                    return group_name

            # Chat default group
            if chat_id and chat_id in self._data["chats"]:
                return self._data["chats"][chat_id]

            return None

    def is_allowed(self, sender_id: str, chat_id: str) -> bool:
        """Check if a request is allowed (any group membership = allowed)."""
        return self.get_group(sender_id, chat_id) is not None

    def is_admin(self, sender_id: str) -> bool:
        """Check if sender is admin."""
        with self._lock:
            return sender_id in self._data["admins"]

    # ── Sudo (admin testing) ──

    def set_sudo(self, chat_id: str, group_name: str) -> None:
        """Set sudo override for a chat (admin testing only)."""
        with self._lock:
            self._sudo_overrides[chat_id] = group_name

    def clear_sudo(self, chat_id: str) -> None:
        """Clear sudo override for a chat."""
        with self._lock:
            self._sudo_overrides.pop(chat_id, None)

    def get_sudo(self, chat_id: str) -> str | None:
        """Get current sudo override for a chat."""
        with self._lock:
            return self._sudo_overrides.get(chat_id)

    # ── Group mutation methods ──

    def set_group(self, open_id: str, group_name: str) -> bool:
        """Assign a user to a permission group. Removes from previous group.

        Returns True if group exists and assignment succeeded.
        """
        with self._lock:
            # Validate group exists (admin is always valid)
            if group_name != "admin" and group_name not in self._group_configs:
                return False

            # Remove from admins if present
            if open_id in self._data["admins"]:
                self._data["admins"].remove(open_id)

            # Remove from all named groups
            for members in self._data["groups"].values():
                if open_id in members:
                    members.remove(open_id)

            # Add to target group
            if group_name == "admin":
                self._data["admins"].append(open_id)
            else:
                if group_name not in self._data["groups"]:
                    self._data["groups"][group_name] = []
                self._data["groups"][group_name].append(open_id)

            self._save()
            return True

    def remove_from_group(self, open_id: str) -> str | None:
        """Remove a user from their current group. Returns previous group name."""
        with self._lock:
            if open_id in self._data["admins"]:
                self._data["admins"].remove(open_id)
                self._save()
                return "admin"
            for group_name, members in self._data["groups"].items():
                if open_id in members:
                    members.remove(open_id)
                    self._save()
                    return group_name
            return None

    # ── Chat group methods ──

    def set_chat_group(self, chat_id: str, group_name: str) -> bool:
        """Set the default permission group for a chat."""
        with self._lock:
            if group_name not in self._group_configs:
                return False
            self._data["chats"][chat_id] = group_name
            self._save()
            return True

    def remove_chat(self, chat_id: str) -> None:
        with self._lock:
            if chat_id in self._data["chats"]:
                del self._data["chats"][chat_id]
                self._save()

    # ── Query helpers ──

    def list_all(self) -> dict:
        """List all permission assignments."""
        with self._lock:
            return {
                "admins": list(self._data["admins"]),
                "groups": {k: list(v) for k, v in self._data["groups"].items()},
                "chats": dict(self._data["chats"]),
            }

    # ── Role methods (unchanged) ──

    def set_role(self, chat_id: str, open_id: str, name: str, desc: str) -> None:
        """Set or update a member's role in a group chat."""
        with self._lock:
            if chat_id not in self._data["roles"]:
                self._data["roles"][chat_id] = {}
            self._data["roles"][chat_id][open_id] = {"name": name, "desc": desc}
            self._save()

    def remove_role(self, chat_id: str, open_id: str) -> bool:
        """Remove a member's role. Returns True if found and removed."""
        with self._lock:
            chat_roles = self._data["roles"].get(chat_id, {})
            if open_id in chat_roles:
                del chat_roles[open_id]
                if not chat_roles:
                    del self._data["roles"][chat_id]
                self._save()
                return True
            return False

    def get_roles(self, chat_id: str) -> dict[str, dict]:
        """Get all member roles for a group. Returns {open_id: {"name": str, "desc": str}}."""
        with self._lock:
            return dict(self._data["roles"].get(chat_id, {}))
