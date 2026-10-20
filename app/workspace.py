import logging
import shutil
from pathlib import Path

PROJECT_SKILLS_DIRECTORY = Path(__file__).resolve().parent.parent / "skills"
REQUIRED_SKILLS = ("temporal-developer", "temporal-workflow-design-critic")
REPO_DIRECTORY_NAME = "repo"
WORKSPACE_SKILLS_PATH = Path(".claude") / "skills"

logger = logging.getLogger(__name__)


class WorkspaceRepoMissingError(RuntimeError):
    pass


def available_skills(skills_source: Path = PROJECT_SKILLS_DIRECTORY) -> list[str]:
    return sorted(
        child.name
        for child in skills_source.iterdir()
        if child.is_dir() and (child / "SKILL.md").is_file()
    ) if skills_source.is_dir() else []


def missing_skills(skills_source: Path = PROJECT_SKILLS_DIRECTORY) -> list[str]:
    present = set(available_skills(skills_source))
    return [name for name in REQUIRED_SKILLS if name not in present]


def warn_about_missing_skills(skills_source: Path = PROJECT_SKILLS_DIRECTORY) -> list[str]:
    missing = missing_skills(skills_source)
    for name in missing:
        logger.warning(
            "Skill %s is missing; place it at %s/SKILL.md. Scans will run without it.",
            name,
            skills_source / name,
        )
    return missing


def prepare_workspace_directory(workspace: Path, skills_source: Path = PROJECT_SKILLS_DIRECTORY) -> list[str]:
    if not (workspace / REPO_DIRECTORY_NAME).is_dir():
        raise WorkspaceRepoMissingError(f"No cloned repository at {workspace / REPO_DIRECTORY_NAME}")
    warn_about_missing_skills(skills_source)
    skills_target = workspace / WORKSPACE_SKILLS_PATH
    if skills_target.exists():
        shutil.rmtree(skills_target)
    skills_target.mkdir(parents=True)
    copied = available_skills(skills_source)
    for name in copied:
        shutil.copytree(skills_source / name, skills_target / name, symlinks=False)
    return copied
