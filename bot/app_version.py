"""Identity of the Docker image: git commit baked in at compose build."""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

BUILD_GIT_PATH = Path("/app/.build-git")
_HERE = Path(__file__).resolve().parent
_REPO_BUILD_GIT = _HERE / ".build-git"
_MERGE_TITLE = re.compile(
    r"^Merge (?:branch|pull request|remote-tracking branch)\b",
    re.IGNORECASE,
)


def parse_build_git(text: str) -> tuple[str, str]:
    commit = "unknown"
    title = "unknown"
    for line in text.splitlines():
        if line.startswith("commit="):
            commit = line[7:].strip() or "unknown"
        elif line.startswith("title="):
            title = line[6:].strip() or "unknown"
    return commit, title


def is_merge_commit_title(title: str) -> bool:
    return bool(_MERGE_TITLE.match(title.strip()))


def _from_file(path: Path) -> tuple[str, str] | None:
    try:
        if path.is_file():
            return parse_build_git(path.read_text(encoding="utf-8"))
    except OSError:
        return None
    return None


def _env(name: str) -> str:
    return os.getenv(name, "").strip()


def _git_dirs() -> list[Path]:
    seen: set[Path] = set()
    dirs: list[Path] = []
    for raw in (_HERE, _HERE.parent, Path("/host")):
        path = raw.resolve() if raw.exists() else raw
        if path in seen:
            continue
        seen.add(path)
        dirs.append(path)
    return dirs


def non_merge_title_from_git() -> str | None:
    """Latest non-merge subject, if git and a checkout are available."""
    for repo in _git_dirs():
        git_dir = repo / ".git"
        if not git_dir.exists():
            continue
        try:
            result = subprocess.run(
                [
                    "git",
                    "-C",
                    str(repo),
                    "log",
                    "-1",
                    "--first-parent",
                    "--no-merges",
                    "--pretty=%s",
                ],
                check=False,
                capture_output=True,
                text=True,
                timeout=2,
            )
        except (OSError, subprocess.TimeoutExpired):
            continue
        title = (result.stdout or "").strip()
        if result.returncode == 0 and title and not is_merge_commit_title(title):
            return title
    return None


def app_build_identity() -> tuple[str, str]:
    """Return (short_commit, commit_title) from image build metadata."""
    commit = _env("APP_GIT_COMMIT")
    title = _env("APP_GIT_COMMIT_TITLE")
    if commit in {"", "unknown"} or title in {"", "unknown"}:
        parsed = _from_file(BUILD_GIT_PATH) or _from_file(_REPO_BUILD_GIT)
        if parsed:
            file_commit, file_title = parsed
            if commit in {"", "unknown"}:
                commit = file_commit
            if title in {"", "unknown"}:
                title = file_title
    if is_merge_commit_title(title):
        better = non_merge_title_from_git()
        if better:
            title = better
    return (commit or "unknown", title or "unknown")
