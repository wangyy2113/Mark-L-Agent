"""Biz domain discovery and context loading."""

import logging
from pathlib import Path

from core.config import get_settings

try:
    import yaml
except ImportError:
    yaml = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)


def _resolve_biz_base() -> Path:
    """Resolve biz base path to absolute."""
    p = Path(get_settings().biz_base_path)
    if not p.is_absolute():
        p = Path(__file__).parent.parent / p  # relative to project root, not core/
    return p


def discover_domains() -> list[str]:
    """Scan biz/ dir, return list of domain names."""
    base = _resolve_biz_base()
    if not base.is_dir():
        return []
    return sorted(
        d.name for d in base.iterdir()
        if d.is_dir() and not d.name.startswith((".", "_"))
    )


def load_domain_prompt(domain: str) -> str:
    """Load prompt.md content for a biz domain. Returns '' if not found.

    NOTE: prompt.md is currently unused — domain context comes from
    context/ and knowledge/ directories instead. Kept for future use.
    """
    # path = _resolve_biz_base() / domain / "prompt.md"
    # if path.exists():
    #     return path.read_text(encoding="utf-8").strip()
    return ""


def load_claude_md(domain: str) -> str:
    """Load CLAUDE.md from each repo under repos/. Returns combined content."""
    repos_dir = Path(repos_path(domain))
    if not repos_dir.is_dir():
        return ""
    parts: list[str] = []
    for d in sorted(repos_dir.iterdir()):
        if not d.is_dir():
            continue
        for name in ("CLAUDE.md", "claude.md"):
            p = d / name
            if p.exists():
                content = p.read_text(encoding="utf-8").strip()
                if content:
                    parts.append(f"[{d.name}/CLAUDE.md]\n{content}")
                break
    if parts:
        logger.info("Loaded CLAUDE.md from %d repo(s) for domain=%s", len(parts), domain)
    return "\n\n".join(parts)


def repos_path(domain: str) -> str:
    """Return absolute path to biz/<domain>/repos/ (falls back to projects/)."""
    base = _resolve_biz_base() / domain
    repos = base / "repos"
    if repos.is_dir():
        return str(repos)
    legacy = base / "projects"
    if legacy.is_dir():
        return str(legacy)
    return str(repos)  # default to repos/


# ── New context loading functions ──


def load_base_context(files: list[str] | None = None) -> str:
    """Load .md files from _base/ and _base/context/. files=None loads all, sorted by name."""
    base_dir = _resolve_biz_base() / "_base"
    if not base_dir.is_dir():
        return ""
    if files is not None:
        paths = [base_dir / f for f in files]
    else:
        paths = sorted(base_dir.glob("*.md"))
        # Also load from _base/context/ subdirectory
        ctx_dir = base_dir / "context"
        if ctx_dir.is_dir():
            paths.extend(sorted(ctx_dir.glob("*.md")))
    parts: list[str] = []
    for p in paths:
        if p.exists():
            content = p.read_text(encoding="utf-8").strip()
            if content:
                parts.append(content)
                logger.debug("_base: loaded %s (%d chars)", p.name, len(content))
    logger.debug("load_base_context: %d files, %d chars total", len(parts), sum(len(p) for p in parts))
    return "\n\n".join(parts)


def load_context_dir(domain: str) -> str:
    """Load all .md files from biz/<domain>/context/, sorted by name."""
    ctx_dir = _resolve_biz_base() / domain / "context"
    if not ctx_dir.is_dir():
        return ""
    parts: list[str] = []
    for p in sorted(ctx_dir.glob("*.md")):
        content = p.read_text(encoding="utf-8").strip()
        if content:
            parts.append(content)
    return "\n\n".join(parts)


def load_knowledge_index(domain: str) -> str | None:
    """Load biz/<domain>/knowledge/index.md. Returns None if not found."""
    p = _resolve_biz_base() / domain / "knowledge" / "index.md"
    if p.exists():
        return p.read_text(encoding="utf-8").strip() or None
    return None


def load_knowledge_overview(domain: str) -> str | None:
    """Load biz/<domain>/knowledge/overview.md. Returns None if not found."""
    p = _resolve_biz_base() / domain / "knowledge" / "overview.md"
    if p.exists():
        return p.read_text(encoding="utf-8").strip() or None
    return None


def has_knowledge(domain: str) -> bool:
    """Check if biz/<domain>/knowledge/ exists and has content."""
    kdir = _resolve_biz_base() / domain / "knowledge"
    if not kdir.is_dir():
        return False
    return any(kdir.iterdir())


def load_domain_yaml(domain: str) -> dict:
    """Load biz/<domain>/domain.yaml. Returns empty dict if not found or yaml unavailable."""
    if yaml is None:
        return {}
    p = _resolve_biz_base() / domain / "domain.yaml"
    if not p.exists():
        return {}
    try:
        data = yaml.safe_load(p.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        logger.warning("Failed to parse domain.yaml for %s", domain, exc_info=True)
        return {}


def load_domain_context(domains: list[str], include_base: bool = True) -> dict:
    """Load full context for given domains. Returns dict for prompt builders.

    Keys:
        base_context, domains, domain_prompt, claude_md, context,
        knowledge_index, knowledge_overview, has_knowledge,
        repos_path, repos_paths
    """
    base_ctx = load_base_context() if include_base else ""

    prompts: list[str] = []
    claude_mds: list[str] = []
    contexts: list[str] = []
    ki_parts: list[str] = []
    ko_parts: list[str] = []
    any_knowledge = False
    r_paths: list[str] = []

    for d in domains:
        dp = load_domain_prompt(d)
        if dp:
            prompts.append(dp if len(domains) == 1 else f"[{d}]\n{dp}")

        cm = load_claude_md(d)
        if cm:
            claude_mds.append(cm)

        ctx = load_context_dir(d)
        if ctx:
            contexts.append(ctx if len(domains) == 1 else f"[{d}]\n{ctx}")

        ki = load_knowledge_index(d)
        if ki:
            ki_parts.append(ki if len(domains) == 1 else f"[{d}]\n{ki}")

        ko = load_knowledge_overview(d)
        if ko:
            ko_parts.append(ko if len(domains) == 1 else f"[{d}]\n{ko}")

        if has_knowledge(d):
            any_knowledge = True

        r_paths.append(repos_path(d))

    result = {
        "base_context": base_ctx,
        "domains": domains,
        "biz_base": str(_resolve_biz_base()),
        "domain_prompt": "\n\n".join(prompts),
        "claude_md": "\n\n".join(claude_mds),
        "context": "\n\n".join(contexts),
        "knowledge_index": "\n\n".join(ki_parts) if ki_parts else None,
        "knowledge_overview": "\n\n".join(ko_parts) if ko_parts else None,
        "has_knowledge": any_knowledge,
        "repos_path": r_paths[0] if r_paths else "",
        "repos_paths": r_paths,
    }
    logger.debug(
        "load_domain_context(%s): base=%d, prompt=%d, claude_md=%d, context=%d, "
        "knowledge=%s, overview=%d, index=%d",
        domains, len(base_ctx), len(result["domain_prompt"]),
        len(result["claude_md"]), len(result["context"]),
        any_knowledge,
        len(result["knowledge_overview"] or ""),
        len(result["knowledge_index"] or ""),
    )
    return result
