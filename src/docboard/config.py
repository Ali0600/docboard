"""The projects allowlist (config/projects.toml). Nothing outside it is ever read."""

from __future__ import annotations

import os
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG = REPO_ROOT / "config" / "projects.toml"
_GITHUB = re.compile(r"^[A-Za-z0-9-]+/[A-Za-z0-9._-]+$")
_GLOB = re.compile(r"^[A-Za-z0-9._-]+(?:/[A-Za-z0-9._-]+)*/\*$")


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class Subprojects:
    """Sub-projects are the directories matching `glob` ("games/*"), less `exclude`."""

    glob: str
    exclude: tuple[str, ...] = ()
    name_from: str | None = None  # "game.toml:name": a TOML file in the sub-project and its key
    aliases: dict[str, tuple[str, ...]] = field(default_factory=dict)
    branches: dict[str, tuple[str, ...]] = field(default_factory=dict)

    @property
    def parent(self) -> str:
        return self.glob[:-2]


@dataclass(frozen=True)
class Project:
    name: str
    path: Path
    github: str | None = None
    docs_ref: str | None = None  # None: origin/HEAD's branch if there is one, else the working tree
    root_name: str = "Platform"
    subprojects: Subprojects | None = None


@dataclass(frozen=True)
class Config:
    projects: tuple[Project, ...]
    claude_home: Path

    @property
    def transcripts_dir(self) -> Path:
        return self.claude_home / "projects"

    @property
    def plans_dir(self) -> Path:
        return self.claude_home / "plans"

    def project(self, name: str) -> Project:
        for p in self.projects:
            if p.name == name:
                return p
        raise ConfigError(f"no project named {name!r} in the config")


def _strs(value: object, where: str) -> tuple[str, ...]:
    if isinstance(value, str):
        return (value,)
    if isinstance(value, list) and all(isinstance(v, str) for v in value):
        return tuple(value)
    raise ConfigError(f"{where}: expected a string or a list of strings")


def _str_map(value: object, where: str) -> dict[str, tuple[str, ...]]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ConfigError(f"{where}: expected a table")
    return {str(k): _strs(v, f"{where}.{k}") for k, v in value.items()}


def parse(data: dict, base: Path) -> Config:
    home = Path(os.path.expanduser(str(data.get("claude_home", "~/.claude"))))
    raw = data.get("project", [])
    if not isinstance(raw, list) or not raw:
        raise ConfigError("the config lists no [[project]]")
    projects: list[Project] = []
    for i, p in enumerate(raw):
        where = f"project[{i}]"
        if not isinstance(p, dict) or "path" not in p:
            raise ConfigError(f"{where}: needs a path")
        path = Path(os.path.expanduser(str(p["path"])))
        if not path.is_absolute():
            path = (base / path).resolve()
        github = p.get("github")
        if github is not None and not _GITHUB.match(str(github)):
            raise ConfigError(f"{where}.github: expected owner/name, got {github!r}")
        subs = None
        if "subprojects" in p:
            s = p["subprojects"]
            glob = str(s.get("glob", ""))
            if not _GLOB.match(glob):
                raise ConfigError(f"{where}.subprojects.glob: expected 'dir/*', got {glob!r}")
            subs = Subprojects(
                glob=glob,
                exclude=_strs(s.get("exclude", []), f"{where}.subprojects.exclude"),
                name_from=s.get("name_from"),
                aliases=_str_map(s.get("aliases"), f"{where}.subprojects.aliases"),
                branches=_str_map(s.get("branches"), f"{where}.subprojects.branches"),
            )
        projects.append(
            Project(
                name=str(p.get("name", path.name)),
                path=path,
                github=str(github) if github else None,
                docs_ref=p.get("docs_ref"),
                root_name=str(p.get("root_name", "Platform")),
                subprojects=subs,
            )
        )
    names = [p.name for p in projects]
    if len(set(names)) != len(names):
        raise ConfigError(f"project names repeat: {names}")
    return Config(projects=tuple(projects), claude_home=home)


def load(path: Path | None = None) -> Config:
    path = path or Path(os.environ.get("DOCBOARD_CONFIG", DEFAULT_CONFIG))
    if not path.is_file():
        raise ConfigError(
            f"no config at {path}: copy config/projects.example.toml to config/projects.toml"
        )
    with path.open("rb") as fh:
        return parse(tomllib.load(fh), path.parent)
