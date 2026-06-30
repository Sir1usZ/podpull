"""Tests for `podpull skills` — agent integration install/detect/uninstall."""
import os

from podpull import skills


def _fake_home(monkeypatch, tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    # On some platforms expanduser also consults USERPROFILE
    monkeypatch.setenv("USERPROFILE", str(home))
    return home


def test_bundled_files_present():
    # Packaged data is readable (proves resources are bundled / resolvable).
    for name in ("SKILL.md", "opencode_command.md", "cursor_rule.mdc"):
        text = skills._bundled(name)
        assert "podpull" in text and len(text) > 100


def test_detect_only_existing(monkeypatch, tmp_path):
    home = _fake_home(monkeypatch, tmp_path)
    assert skills.detect() == []
    (home / ".claude").mkdir()
    (home / ".config" / "opencode").mkdir(parents=True)
    assert set(skills.detect()) == {"claude", "opencode"}


def test_install_claude_and_codex_write_skill(monkeypatch, tmp_path):
    home = _fake_home(monkeypatch, tmp_path)
    r = skills.install_one("claude")
    assert r.status == "installed"
    p = home / ".claude" / "skills" / "podpull" / "SKILL.md"
    assert p.is_file() and "name: podpull" in p.read_text()
    r2 = skills.install_one("codex")
    assert (home / ".codex" / "skills" / "podpull" / "SKILL.md").is_file()


def test_install_opencode_command(monkeypatch, tmp_path):
    home = _fake_home(monkeypatch, tmp_path)
    skills.install_one("opencode")
    # default dir is commands/ when neither exists
    p = home / ".config" / "opencode" / "commands" / "podpull.md"
    assert p.is_file() and "description:" in p.read_text()


def test_opencode_respects_existing_singular_dir(monkeypatch, tmp_path):
    home = _fake_home(monkeypatch, tmp_path)
    (home / ".config" / "opencode" / "command").mkdir(parents=True)
    skills.install_one("opencode")
    assert (home / ".config" / "opencode" / "command" / "podpull.md").is_file()


def test_cursor_project_rule(monkeypatch, tmp_path):
    _fake_home(monkeypatch, tmp_path)
    proj = tmp_path / "proj"
    (proj / ".git").mkdir(parents=True)
    monkeypatch.chdir(proj)
    r = skills.install_one("cursor")
    assert r.status == "installed"
    assert (proj / ".cursor" / "rules" / "podpull.mdc").is_file()


def test_cursor_manual_when_not_in_project(monkeypatch, tmp_path):
    home = _fake_home(monkeypatch, tmp_path)
    workdir = tmp_path / "elsewhere"
    workdir.mkdir()
    monkeypatch.chdir(workdir)
    r = skills.install_one("cursor")
    assert r.status == "manual"
    assert (home / ".config" / "podpull" / "cursor-podpull.mdc").is_file()


def test_uninstall(monkeypatch, tmp_path):
    home = _fake_home(monkeypatch, tmp_path)
    skills.install_one("claude")
    r = skills.uninstall_one("claude")
    assert r.note == "removed"
    assert not (home / ".claude" / "skills" / "podpull").exists()
    # uninstalling again is a no-op
    assert skills.uninstall_one("claude").status == "skipped"
