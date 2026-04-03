"""Test core permission gate: whitelist, group resolution, admin isolation.

Ensures only authorized users (admin whitelist) can access the bot,
and that group/chat-default boundaries are correctly enforced.

Usage:
    python tests/test_permissions.py
"""

import json
import sys
import tempfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from core.permissions import Permissions, GroupConfig


def _make_perms(data: dict | None = None, configs: dict[str, GroupConfig] | None = None) -> Permissions:
    """Create a Permissions instance backed by a temp file."""
    tmp = tempfile.NamedTemporaryFile(suffix=".json", delete=False)
    if data:
        tmp.write(json.dumps(data).encode())
    tmp.close()
    p = Permissions(path=tmp.name)
    if configs:
        p.set_group_configs(configs)
    # Clean up temp on GC (best-effort)
    p._tmp_path = tmp.name
    return p


ADMIN_ID = "ou_admin_001"
DEV_ID = "ou_dev_001"
STRANGER_ID = "ou_stranger_999"
CHAT_A = "oc_chat_a"
CHAT_B = "oc_chat_b"


# ── is_allowed: whitelist gate ──

def test_unknown_user_denied():
    """Stranger with no group membership is denied."""
    p = _make_perms({"admins": [ADMIN_ID], "groups": {}, "chats": {}})
    assert not p.is_allowed(STRANGER_ID, CHAT_A)


def test_admin_allowed():
    p = _make_perms({"admins": [ADMIN_ID], "groups": {}, "chats": {}})
    assert p.is_allowed(ADMIN_ID, CHAT_A)


def test_group_member_allowed():
    p = _make_perms({"admins": [ADMIN_ID], "groups": {"developer": [DEV_ID]}, "chats": {}})
    assert p.is_allowed(DEV_ID, CHAT_A)


def test_empty_sender_denied():
    """Empty open_id should never be allowed."""
    p = _make_perms({"admins": [ADMIN_ID], "groups": {}, "chats": {}})
    assert not p.is_allowed("", CHAT_A)


def test_chat_default_allows_unknown_user():
    """A chat with a default group lets any user in that chat through."""
    p = _make_perms({"admins": [], "groups": {}, "chats": {CHAT_A: "member"}})
    assert p.is_allowed(STRANGER_ID, CHAT_A)


def test_chat_default_does_not_leak_to_other_chats():
    """Chat default for CHAT_A should not grant access in CHAT_B."""
    p = _make_perms({"admins": [], "groups": {}, "chats": {CHAT_A: "member"}})
    assert not p.is_allowed(STRANGER_ID, CHAT_B)


def test_no_permissions_file_denies_all():
    """Fresh instance with no data denies everyone."""
    p = _make_perms()
    assert not p.is_allowed(STRANGER_ID, CHAT_A)
    assert not p.is_allowed(ADMIN_ID, CHAT_A)  # no admins seeded


# ── get_group: resolution order ──

def test_admin_resolves_to_admin_group():
    p = _make_perms({"admins": [ADMIN_ID], "groups": {}, "chats": {}})
    assert p.get_group(ADMIN_ID, CHAT_A) == "admin"


def test_named_group_resolves():
    p = _make_perms({"admins": [], "groups": {"developer": [DEV_ID]}, "chats": {}})
    assert p.get_group(DEV_ID, CHAT_A) == "developer"


def test_chat_default_resolves():
    p = _make_perms({"admins": [], "groups": {}, "chats": {CHAT_A: "member"}})
    assert p.get_group(STRANGER_ID, CHAT_A) == "member"


def test_unknown_resolves_to_none():
    p = _make_perms({"admins": [ADMIN_ID], "groups": {}, "chats": {}})
    assert p.get_group(STRANGER_ID, CHAT_A) is None


def test_admin_takes_priority_over_named_group():
    """If user is both admin and in a named group, admin wins."""
    p = _make_perms({"admins": [ADMIN_ID], "groups": {"developer": [ADMIN_ID]}, "chats": {}})
    assert p.get_group(ADMIN_ID, CHAT_A) == "admin"


def test_named_group_takes_priority_over_chat_default():
    """Named group membership beats chat default."""
    p = _make_perms({"admins": [], "groups": {"developer": [DEV_ID]}, "chats": {CHAT_A: "member"}})
    assert p.get_group(DEV_ID, CHAT_A) == "developer"


# ── is_admin: strict check ──

def test_is_admin_true_for_admin():
    p = _make_perms({"admins": [ADMIN_ID], "groups": {}, "chats": {}})
    assert p.is_admin(ADMIN_ID)


def test_is_admin_false_for_developer():
    p = _make_perms({"admins": [ADMIN_ID], "groups": {"developer": [DEV_ID]}, "chats": {}})
    assert not p.is_admin(DEV_ID)


def test_is_admin_false_for_stranger():
    p = _make_perms({"admins": [ADMIN_ID], "groups": {}, "chats": {}})
    assert not p.is_admin(STRANGER_ID)


# ── Mutation: set_group / remove_from_group ──

def test_remove_from_group_revokes_access():
    """After removal, user should be denied."""
    p = _make_perms({"admins": [], "groups": {"developer": [DEV_ID]}, "chats": {}})
    assert p.is_allowed(DEV_ID, CHAT_A)
    p.remove_from_group(DEV_ID)
    assert not p.is_allowed(DEV_ID, CHAT_A)


def test_set_group_moves_user():
    """set_group removes from old group and adds to new."""
    p = _make_perms({"admins": [], "groups": {"developer": [DEV_ID]}, "chats": {}})
    assert p.get_group(DEV_ID, CHAT_A) == "developer"
    p.set_group(DEV_ID, "admin")
    assert p.get_group(DEV_ID, CHAT_A) == "admin"
    assert p.is_admin(DEV_ID)


def test_set_group_clears_old_membership():
    """After moving to admin, user should not remain in developer group."""
    p = _make_perms({"admins": [], "groups": {"developer": [DEV_ID]}, "chats": {}})
    p.set_group(DEV_ID, "admin")
    assert DEV_ID not in p.list_all()["groups"].get("developer", [])


# ── Sudo: admin-only ──

def test_sudo_overrides_admin_group():
    """Admin with sudo active gets the overridden group."""
    p = _make_perms({"admins": [ADMIN_ID], "groups": {}, "chats": {}})
    p.set_sudo(CHAT_A, "member")
    assert p.get_group(ADMIN_ID, CHAT_A) == "member"


def test_sudo_does_not_affect_non_admin():
    """Non-admin should not benefit from sudo override."""
    p = _make_perms({"admins": [ADMIN_ID], "groups": {}, "chats": {}})
    p.set_sudo(CHAT_A, "developer")
    # Stranger still gets None, not "developer"
    assert p.get_group(STRANGER_ID, CHAT_A) is None


def test_sudo_clear_restores_admin():
    p = _make_perms({"admins": [ADMIN_ID], "groups": {}, "chats": {}})
    p.set_sudo(CHAT_A, "member")
    assert p.get_group(ADMIN_ID, CHAT_A) == "member"
    p.clear_sudo(CHAT_A)
    assert p.get_group(ADMIN_ID, CHAT_A) == "admin"


def test_sudo_scoped_to_chat():
    """Sudo in CHAT_A should not affect CHAT_B."""
    p = _make_perms({"admins": [ADMIN_ID], "groups": {}, "chats": {}})
    p.set_sudo(CHAT_A, "member")
    assert p.get_group(ADMIN_ID, CHAT_A) == "member"
    assert p.get_group(ADMIN_ID, CHAT_B) == "admin"


# ── seed_admin: first-run only ──

def test_seed_admin_on_fresh_instance():
    """seed_admin adds admin when no permissions.json existed."""
    import os
    tmp = tempfile.mktemp(suffix=".json")  # path that doesn't exist yet
    p = Permissions(path=tmp)
    p.seed_admin(ADMIN_ID)
    assert p.is_admin(ADMIN_ID)
    assert p.is_allowed(ADMIN_ID, CHAT_A)
    os.unlink(tmp)  # cleanup


def test_seed_admin_noop_when_file_existed():
    """seed_admin should not add admin when permissions.json was loaded."""
    p = _make_perms({"admins": [], "groups": {}, "chats": {}})
    p.seed_admin(ADMIN_ID)
    # File existed (even if empty), so seed should be a no-op
    assert not p.is_admin(ADMIN_ID)


def test_seed_admin_ignores_empty_id():
    p = _make_perms()
    p.seed_admin("")
    assert p.list_all()["admins"] == []


# ── Chat default: boundary ──

def test_set_chat_group_validates_config():
    """set_chat_group should reject group names not in config."""
    p = _make_perms({"admins": [], "groups": {}, "chats": {}})
    # "nonexistent" is not in DEFAULT_GROUPS
    assert not p.set_chat_group(CHAT_A, "nonexistent")


def test_remove_chat_revokes_default():
    """After removing chat default, unknown users are denied."""
    p = _make_perms({"admins": [], "groups": {}, "chats": {CHAT_A: "member"}})
    assert p.is_allowed(STRANGER_ID, CHAT_A)
    p.remove_chat(CHAT_A)
    assert not p.is_allowed(STRANGER_ID, CHAT_A)


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    passed = failed = 0
    print(f"Running {len(tests)} permission tests...\n")
    for test in tests:
        try:
            test()
            print(f"  ✓ {test.__name__}")
            passed += 1
        except AssertionError as e:
            print(f"  ✗ {test.__name__}: {e}")
            failed += 1
        except Exception as e:
            print(f"  ✗ {test.__name__}: {type(e).__name__}: {e}")
            failed += 1
    print(f"\n{passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)
