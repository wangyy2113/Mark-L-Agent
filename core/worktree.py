"""Git worktree isolation for per-chat development sessions.

Creates isolated worktrees under biz/<domain>/worktrees/<chat_id>/
so concurrent dev sessions don't conflict on shared repos.
"""

import logging
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

from core.biz import repos_path as _biz_repos_path, _resolve_biz_base

logger = logging.getLogger(__name__)

_WORKTREE_DIR = "worktrees"


def _safe_chat_id(chat_id: str) -> str:
    """Sanitize chat_id for use as a directory name."""
    return re.sub(r"[^\w\-]", "_", chat_id)


def _worktrees_base(domain: str) -> Path:
    """Return biz/<domain>/worktrees/."""
    return _resolve_biz_base() / domain / _WORKTREE_DIR


def _worktree_dir(domain: str, chat_id: str) -> Path:
    """Return biz/<domain>/worktrees/<safe_chat_id>/."""
    return _worktrees_base(domain) / _safe_chat_id(chat_id)


def detect_default_branch(repo_path: str) -> str:
    """Detect the default branch (main or master) for a git repo.

    Checks remote HEAD first, then falls back to probing origin/main and origin/master.
    Used by both worktree creation and push/MR workflows.
    """
    try:
        r = subprocess.run(
            ["git", "symbolic-ref", "refs/remotes/origin/HEAD", "--short"],
            cwd=repo_path, capture_output=True, text=True, timeout=5,
        )
        if r.returncode == 0 and r.stdout.strip():
            return r.stdout.strip().split("/")[-1]
    except Exception:
        pass
    for branch in ("main", "master"):
        try:
            r = subprocess.run(
                ["git", "rev-parse", "--verify", f"origin/{branch}"],
                cwd=repo_path, capture_output=True, text=True, timeout=5,
            )
            if r.returncode == 0:
                return branch
        except Exception:
            pass
    return "master"


def _delete_wt_branches(repo_path: str, branch_prefix: str) -> None:
    """Delete worktree carrier branches matching prefix (e.g. 'wt/oc_abc123/').

    Only deletes wt/* branches — never touches feature branches like 20260323-*.
    Uses -D (force) since the worktree is already removed and the branch
    may have unmerged commits from before the agent switched to a feature branch.
    """
    try:
        result = subprocess.run(
            ["git", "branch", "--list", f"{branch_prefix}*"],
            cwd=repo_path, capture_output=True, text=True, timeout=10,
        )
        if result.returncode != 0 or not result.stdout.strip():
            return
        for line in result.stdout.strip().splitlines():
            branch = line.strip().lstrip("* ")
            if not branch.startswith("wt/"):
                continue
            subprocess.run(
                ["git", "branch", "-D", branch],
                cwd=repo_path, capture_output=True, text=True, timeout=10,
            )
            logger.info("Deleted worktree branch: %s in %s", branch, repo_path)
    except Exception:
        logger.warning("Error deleting wt branches in %s", repo_path, exc_info=True)


def get_worktree_path(domain: str, chat_id: str) -> str | None:
    """Return the worktree directory path if it exists, None otherwise."""
    wt_dir = _worktree_dir(domain, chat_id)
    if wt_dir.is_dir():
        return str(wt_dir)
    return None


def touch_worktree(domain: str, chat_id: str) -> None:
    """Update the worktree timestamp to prevent stale cleanup."""
    wt_base = _worktree_dir(domain, chat_id)
    meta = wt_base / ".created_at"
    if wt_base.is_dir():
        try:
            meta.write_text(str(time.time()))
        except OSError:
            pass


def create_worktrees(domain: str, chat_id: str) -> str | None:
    """Create worktrees for all git repos under a domain.

    Returns the worktree base directory path, or None on failure.
    """
    repos_dir = Path(_biz_repos_path(domain))
    if not repos_dir.is_dir():
        logger.warning("repos dir not found for domain=%s", domain)
        return None

    wt_base = _worktree_dir(domain, chat_id)
    safe_id = _safe_chat_id(chat_id)
    branch_prefix = f"wt/{safe_id}"

    created_any = False
    for d in sorted(repos_dir.iterdir()):
        if not d.is_dir() or not (d / ".git").exists():
            continue

        wt_path = wt_base / d.name
        if wt_path.exists():
            # Already exists, skip
            created_any = True
            continue

        default_branch = detect_default_branch(str(d))
        wt_branch = f"{branch_prefix}/{d.name}"

        try:
            wt_path.parent.mkdir(parents=True, exist_ok=True)

            # Create worktree with a new branch based on default branch
            result = subprocess.run(
                ["git", "worktree", "add", "-b", wt_branch, str(wt_path), default_branch],
                cwd=str(d), capture_output=True, text=True, timeout=30,
            )
            if result.returncode != 0:
                # Branch may already exist — try without -b
                result = subprocess.run(
                    ["git", "worktree", "add", str(wt_path), wt_branch],
                    cwd=str(d), capture_output=True, text=True, timeout=30,
                )
            if result.returncode == 0:
                created_any = True
                logger.info("Created worktree: %s → %s (branch %s)", d.name, wt_path, wt_branch)
            else:
                logger.error("Failed to create worktree for %s: %s", d.name, result.stderr.strip())
        except Exception:
            logger.exception("Error creating worktree for %s", d.name)

    if created_any:
        # Write a metadata file for staleness tracking
        meta = wt_base / ".created_at"
        meta.write_text(str(time.time()))
        return str(wt_base)

    return None


def remove_worktrees(domain: str, chat_id: str) -> None:
    """Remove all worktrees for a chat session and clean up the directory."""
    repos_dir = Path(_biz_repos_path(domain))
    wt_base = _worktree_dir(domain, chat_id)

    if not wt_base.is_dir():
        return

    # Remove each worktree via git
    for d in sorted(repos_dir.iterdir()) if repos_dir.is_dir() else []:
        if not d.is_dir() or not (d / ".git").exists():
            continue
        wt_path = wt_base / d.name
        if not wt_path.exists():
            continue
        try:
            subprocess.run(
                ["git", "worktree", "remove", "--force", str(wt_path)],
                cwd=str(d), capture_output=True, text=True, timeout=15,
            )
            logger.info("Removed worktree: %s → %s", d.name, wt_path)
        except Exception:
            logger.exception("Error removing worktree for %s", d.name)

    # Clean up the directory (in case git worktree remove left remnants)
    try:
        if wt_base.is_dir():
            shutil.rmtree(wt_base)
            logger.info("Cleaned up worktree dir: %s", wt_base)
    except Exception:
        logger.exception("Error cleaning up worktree dir: %s", wt_base)

    # Prune stale worktree references and delete carrier branches in each repo
    safe_id = _safe_chat_id(chat_id)
    branch_prefix = f"wt/{safe_id}/"
    for d in sorted(repos_dir.iterdir()) if repos_dir.is_dir() else []:
        if not d.is_dir() or not (d / ".git").exists():
            continue
        try:
            subprocess.run(
                ["git", "worktree", "prune"],
                cwd=str(d), capture_output=True, text=True, timeout=10,
            )
        except Exception:
            pass
        _delete_wt_branches(str(d), branch_prefix)


def check_dirty_state(domain: str, chat_id: str) -> list[dict]:
    """Check if worktree repos have uncommitted changes.

    Returns a list of dicts with 'repo' and 'status' keys for dirty repos.
    """
    wt_base = _worktree_dir(domain, chat_id)
    if not wt_base.is_dir():
        return []

    dirty = []
    for d in sorted(wt_base.iterdir()):
        if not d.is_dir() or not (d / ".git").exists():
            continue
        try:
            result = subprocess.run(
                ["git", "status", "--porcelain"],
                cwd=str(d), capture_output=True, text=True, timeout=10,
            )
            if result.returncode == 0 and result.stdout.strip():
                dirty.append({
                    "repo": d.name,
                    "status": result.stdout.strip()[:200],
                })
        except Exception:
            pass

    return dirty


def _is_dir_dirty(wt_dir: Path) -> bool:
    """Check if any git repo under a worktree directory has uncommitted changes."""
    for d in sorted(wt_dir.iterdir()):
        if not d.is_dir() or not (d / ".git").exists():
            continue
        try:
            result = subprocess.run(
                ["git", "status", "--porcelain"],
                cwd=str(d), capture_output=True, text=True, timeout=10,
            )
            if result.returncode == 0 and result.stdout.strip():
                return True
        except Exception:
            pass
    return False


def cleanup_stale_worktrees(domain: str, max_age_hours: float = 24) -> int:
    """Remove worktrees older than max_age_hours.

    Skips worktrees that have uncommitted changes (dirty state).
    Returns the number of worktree directories removed.
    """
    wt_root = _worktrees_base(domain)
    if not wt_root.is_dir():
        return 0

    now = time.time()
    max_age_secs = max_age_hours * 3600
    removed = 0

    for entry in list(wt_root.iterdir()):
        if not entry.is_dir():
            continue

        # Check last-active timestamp
        meta = entry / ".created_at"
        ts = 0.0
        if meta.exists():
            try:
                ts = float(meta.read_text().strip())
            except (ValueError, OSError):
                pass

        if ts == 0.0:
            # Fallback to directory mtime
            try:
                ts = entry.stat().st_mtime
            except OSError:
                continue

        age = now - ts
        if age < max_age_secs:
            continue

        # Skip dirty worktrees — uncommitted changes should not be silently destroyed
        if _is_dir_dirty(entry):
            logger.warning(
                "Skipping stale worktree cleanup (dirty): %s (age=%.1fh)",
                entry, age / 3600,
            )
            continue

        logger.info("Cleaning stale worktree: %s (age=%.1fh)", entry, age / 3600)

        # Direct cleanup since we have the path
        repos_dir = Path(_biz_repos_path(domain))
        # entry.name is the safe_chat_id used as directory name
        branch_prefix = f"wt/{entry.name}/"
        for d in sorted(repos_dir.iterdir()) if repos_dir.is_dir() else []:
            if not d.is_dir() or not (d / ".git").exists():
                continue
            wt_path = entry / d.name
            if not wt_path.exists():
                continue
            try:
                subprocess.run(
                    ["git", "worktree", "remove", "--force", str(wt_path)],
                    cwd=str(d), capture_output=True, text=True, timeout=15,
                )
            except Exception:
                pass
            try:
                subprocess.run(
                    ["git", "worktree", "prune"],
                    cwd=str(d), capture_output=True, text=True, timeout=10,
                )
            except Exception:
                pass
            _delete_wt_branches(str(d), branch_prefix)

        try:
            if entry.is_dir():
                shutil.rmtree(entry)
            removed += 1
        except Exception:
            logger.exception("Failed to remove stale worktree: %s", entry)

    return removed
