"""Install podpull's agent integrations into supported AI coding agents.

Each agent gets podpull's instructions in its own native format:

  Claude Code  ~/.claude/skills/podpull/SKILL.md          (skill)
  Codex        ~/.codex/skills/podpull/SKILL.md            (skill, same format)
  OpenCode     ~/.config/opencode/command(s)/podpull.md    (slash command /podpull)
  Cursor       <project>/.cursor/rules/podpull.mdc         (project rule)

Cursor has no file-based *global* rule (User Rules are settings-only), so the
Cursor integration installs a project rule when run inside a project, otherwise
it drops the rule file in ~/.config/podpull/ and tells you what to do with it.

All writes are idempotent — re-running (e.g. after `brew upgrade`) just refreshes
the files. Nothing is written unless you run `podpull skills install`.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from importlib import resources

def _home() -> str:
    return os.path.expanduser("~")


def _xdg_config() -> str:
    return os.environ.get("XDG_CONFIG_HOME") or os.path.join(_home(), ".config")


AGENTS = ("claude", "codex", "opencode", "cursor")
_LABELS = {"claude": "Claude Code", "codex": "Codex",
           "opencode": "OpenCode", "cursor": "Cursor"}


@dataclass
class Result:
    agent: str
    status: str          # "installed" | "manual" | "skipped"
    path: str = ""
    note: str = ""

    @property
    def label(self) -> str:
        return _LABELS.get(self.agent, self.agent)


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _bundled(name: str) -> str:
    return resources.files("podpull.integrations").joinpath(name).read_text(encoding="utf-8")


def _write(path: str, content: str) -> str:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    return path


def _config_dir(agent: str) -> str:
    return {
        "claude": os.path.join(_home(), ".claude"),
        "codex": os.path.join(_home(), ".codex"),
        "opencode": os.path.join(_xdg_config(), "opencode"),
        "cursor": os.path.join(_home(), ".cursor"),
    }[agent]


def detect() -> list[str]:
    """Agents whose config directory exists on this machine."""
    return [a for a in AGENTS if os.path.isdir(_config_dir(a))]


def _target_path(agent: str, project: bool = False) -> str:
    """Where the integration file lives (for status/uninstall)."""
    if agent in ("claude", "codex"):
        return os.path.join(_config_dir(agent), "skills", "podpull", "SKILL.md")
    if agent == "opencode":
        base = _config_dir("opencode")
        sub = "command" if os.path.isdir(os.path.join(base, "command")) else "commands"
        return os.path.join(base, sub, "podpull.md")
    if agent == "cursor":
        if project or _in_project():
            return os.path.join(os.getcwd(), ".cursor", "rules", "podpull.mdc")
        return os.path.join(_xdg_config(), "podpull", "cursor-podpull.mdc")
    raise ValueError(agent)


def _in_project() -> bool:
    cwd = os.getcwd()
    return os.path.isdir(os.path.join(cwd, ".cursor")) or os.path.isdir(os.path.join(cwd, ".git"))


# --------------------------------------------------------------------------- #
# install / uninstall / status
# --------------------------------------------------------------------------- #
def install_one(agent: str, *, project: bool = False) -> Result:
    if agent in ("claude", "codex"):
        p = _write(_target_path(agent), _bundled("SKILL.md"))
        return Result(agent, "installed", p)
    if agent == "opencode":
        p = _write(_target_path(agent), _bundled("opencode_command.md"))
        return Result(agent, "installed", p, note="invoke with /podpull")
    if agent == "cursor":
        if project or _in_project():
            p = _write(_target_path(agent, project=True), _bundled("cursor_rule.mdc"))
            return Result(agent, "installed", p, note="project rule")
        # No file-based global rule in Cursor — drop the file where the user can grab it.
        p = _write(_target_path(agent), _bundled("cursor_rule.mdc"))
        return Result(agent, "manual", p,
                      note="Cursor global rules are settings-only. Copy this into a project's "
                           ".cursor/rules/ (or run `podpull skills install` inside a project), "
                           "or paste it into Cursor Settings → Rules.")
    raise ValueError(f"unknown agent: {agent}")


def install(agents: list[str] | None = None, *, project: bool = False) -> list[Result]:
    return [install_one(a, project=project) for a in (agents or detect())]


def uninstall_one(agent: str, *, project: bool = False) -> Result:
    path = _target_path(agent, project=project)
    if os.path.exists(path):
        os.remove(path)
        # tidy now-empty podpull skill dir
        parent = os.path.dirname(path)
        if agent in ("claude", "codex") and os.path.basename(parent) == "podpull":
            try:
                os.rmdir(parent)
            except OSError:
                pass
        return Result(agent, "installed", path, note="removed")
    return Result(agent, "skipped", path, note="not installed")


def status() -> list[Result]:
    out = []
    detected = set(detect())
    for a in AGENTS:
        path = _target_path(a)
        installed = os.path.exists(path)
        out.append(Result(
            a,
            "installed" if installed else ("skipped"),
            path if installed else "",
            note=("detected" if a in detected else "not detected"),
        ))
    return out
