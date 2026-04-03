"""Skill system — reusable workflows that agents can invoke.

Inspired by Claude Code's skill architecture:
- Bundled skills: registered in code, always available
- Disk skills: SKILL.md files in skills/ directory, auto-discovered
- Skills are injected into agent system prompts as available workflows

A skill is a structured prompt template with metadata (name, description,
trigger keywords). When the agent detects a matching user intent, it
follows the skill's workflow steps.

Usage:
    from core.skills import load_skills, get_skill, list_skills, build_skills_prompt

    # At startup
    load_skills()

    # In prompt builder
    prompt += build_skills_prompt()

    # Lookup specific skill
    skill = get_skill("generate-knowledge")
"""

import logging
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass
class Skill:
    """A reusable workflow definition."""
    name: str
    description: str
    prompt: str                          # Full workflow prompt (from SKILL.md body)
    trigger_keywords: list[str] = field(default_factory=list)  # Keywords that activate this skill
    source: str = "disk"                 # "disk" or "bundled"
    references: dict[str, str] = field(default_factory=dict)   # filename → content


# ── Registry ──

_skills: dict[str, Skill] = {}


def register(skill: Skill) -> None:
    """Register a skill. Called at startup."""
    _skills[skill.name] = skill
    logger.info("Registered skill: %s (%s)", skill.name, skill.source)


def get_skill(name: str) -> Skill | None:
    """Get a skill by name."""
    return _skills.get(name)


def list_skills() -> list[Skill]:
    """List all registered skills."""
    return list(_skills.values())


# ── Disk skill loader ──

def _parse_skill_md(path: Path) -> Skill | None:
    """Parse a SKILL.md file into a Skill object.

    Expected format:
        ---
        name: skill-name
        description: What this skill does
        ---
        # Skill Title
        ... workflow content ...
    """
    try:
        content = path.read_text(encoding="utf-8")
    except Exception:
        logger.warning("Failed to read skill file: %s", path)
        return None

    # Parse YAML frontmatter
    name = ""
    description = ""
    prompt = content

    if content.startswith("---"):
        parts = content.split("---", 2)
        if len(parts) >= 3:
            frontmatter = parts[1].strip()
            prompt = parts[2].strip()
            for line in frontmatter.splitlines():
                if line.startswith("name:"):
                    name = line.split(":", 1)[1].strip()
                elif line.startswith("description:"):
                    description = line.split(":", 1)[1].strip()

    if not name:
        name = path.parent.name  # Use directory name as fallback

    # Load reference files
    references: dict[str, str] = {}
    refs_dir = path.parent / "references"
    if refs_dir.is_dir():
        for ref_file in sorted(refs_dir.glob("*.md")):
            try:
                references[ref_file.name] = ref_file.read_text(encoding="utf-8")
            except Exception:
                logger.warning("Failed to read reference: %s", ref_file)

    return Skill(
        name=name,
        description=description,
        prompt=prompt,
        source="disk",
        references=references,
    )


def _discover_disk_skills(skills_dir: str = "") -> list[Skill]:
    """Scan skills/ directory for SKILL.md files."""
    if not skills_dir:
        skills_dir = str(Path(__file__).parent.parent / "skills")
    base = Path(skills_dir)
    if not base.is_dir():
        return []

    skills = []
    for skill_dir in sorted(base.iterdir()):
        if not skill_dir.is_dir() or skill_dir.name.startswith((".", "_")):
            continue
        skill_md = skill_dir / "SKILL.md"
        if skill_md.exists():
            skill = _parse_skill_md(skill_md)
            if skill:
                skills.append(skill)

    return skills


# ── Bundled skills ──

def _register_bundled_skills() -> None:
    """Register built-in skills that ship with mark-l-agent."""

    register(Skill(
        name="code-review",
        description="对代码变更进行 review，检查逻辑错误、安全问题、性能问题",
        prompt=(
            "# Code Review\n\n"
            "对指定的代码变更进行 review。\n\n"
            "## 步骤\n"
            "1. 读取变更文件（git diff 或指定文件）\n"
            "2. 检查以下维度：\n"
            "   - 逻辑正确性\n"
            "   - 边界条件和异常处理\n"
            "   - 安全风险（SQL 注入、XSS、敏感信息泄露）\n"
            "   - 性能影响（N+1 查询、大循环、内存泄漏）\n"
            "   - 代码风格和可读性\n"
            "3. 输出结构化 review 报告\n"
        ),
        trigger_keywords=["review", "代码审查", "code review", "cr"],
        source="bundled",
    ))

    register(Skill(
        name="incident-analysis",
        description="分析线上故障，从监控、日志、代码三个维度定位根因",
        prompt=(
            "# Incident Analysis\n\n"
            "分析线上故障或异常。\n\n"
            "## 步骤\n"
            "1. 明确问题现象（时间、影响范围、错误信息）\n"
            "2. 查看监控指标（CPU、内存、QPS、延迟、错误率）\n"
            "3. 查看相关日志（SLS 或本地日志）\n"
            "4. 关联代码逻辑（找到可能的根因代码）\n"
            "5. 输出分析报告：现象 → 分析 → 根因 → 建议\n"
        ),
        trigger_keywords=["故障", "incident", "排查", "线上问题", "告警"],
        source="bundled",
    ))

    register(Skill(
        name="weekly-report",
        description="生成团队周报，汇总本周工作、告警、代码变更",
        prompt=(
            "# Weekly Report\n\n"
            "生成团队周报。\n\n"
            "## 步骤\n"
            "1. 查看本周 Git commit 记录（各仓库）\n"
            "2. 查看本周告警汇总\n"
            "3. 查看飞书文档中的本周工作记录\n"
            "4. 汇总为结构化周报：\n"
            "   - 本周完成\n"
            "   - 线上稳定性\n"
            "   - 下周计划\n"
        ),
        trigger_keywords=["周报", "weekly", "report", "汇总"],
        source="bundled",
    ))


# ── Public API ──

def load_skills(skills_dir: str = "") -> int:
    """Load all skills (bundled + disk). Call once at startup.

    Returns number of skills loaded.
    """
    _skills.clear()

    # 1. Bundled skills
    _register_bundled_skills()

    # 2. Disk skills (from skills/ directory)
    for skill in _discover_disk_skills(skills_dir):
        register(skill)

    logger.info("Loaded %d skills: %s", len(_skills), list(_skills.keys()))
    return len(_skills)


def build_skills_prompt() -> str:
    """Build a prompt section listing available skills.

    Injected into agent system prompts so the agent knows what
    workflows are available.
    """
    skills = list_skills()
    if not skills:
        return ""

    lines = ["\n## 可用技能（Skills）\n"]
    lines.append("以下是预定义的工作流，当用户意图匹配时可以参考执行：\n")
    for s in skills:
        lines.append(f"- **{s.name}**：{s.description}")

    return "\n".join(lines)
