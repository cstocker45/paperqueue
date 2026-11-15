"""The launcher app, the Desktop symlink, `paperqueue open` and `paperqueue uninstall`."""

from __future__ import annotations

import os
import plistlib
import subprocess
from pathlib import Path

import httpx
import pytest

from paperqueue import __version__, cli, shortcut
from paperqueue.skill import SKILL_MD

CONFIG = '[server]\ntoken = "t"\nport = 8765\n[calendar]\nbackend = "off"\n[pdf]\nenabled = false\n'


class Calls:
    """Fake subprocess.run: records argv and answers with per-command return codes."""

    def __init__(self, codes: dict[tuple[str, ...], int] | None = None):
        self.argv: list[list[str]] = []
        self.codes = codes or {}

    def __call__(self, argv, *a, **k):
        argv = [str(x) for x in argv]
        self.argv.append(argv)
        code = next((c for prefix, c in self.codes.items() if tuple(argv[: len(prefix)]) == prefix), 0)
        return subprocess.CompletedProcess(argv, code, "", "")

    def starts(self, *prefix: str) -> list[list[str]]:
        return [a for a in self.argv if tuple(a[: len(prefix)]) == prefix]


@pytest.fixture
def user(tmp_path, monkeypatch) -> Path:
    home = tmp_path / "user"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setenv("HOME", str(home))
    pq = tmp_path / "pqhome"
    pq.mkdir()
    (pq / "config.toml").write_text(CONFIG)
    monkeypatch.setenv("PAPERQUEUE_HOME", str(pq))

    def no_network(self, request):
        raise httpx.ConnectError("network disabled in tests")

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", no_network)

    def forbid(*a, **k):
        raise AssertionError(f"subprocess called: {a}")

    monkeypatch.setattr(subprocess, "run", forbid)
    return home


def _app(home: Path) -> Path:
    return home / "Applications" / "paperqueue.app"


def test_bundle_contents(user, monkeypatch):
    monkeypatch.setenv("PAPERQUEUE_HOME", "/tmp/a dir/pq")
    rows = shortcut.install_shortcut()
    assert [(r.name, r.status) for r in rows] == [("Launcher app", "done"), ("Desktop shortcut", "done")]
    app = _app(user)
    with (app / "Contents" / "Info.plist").open("rb") as f:
        info = plistlib.load(f)
    assert info["CFBundleIdentifier"] == "io.github.paperqueue.launcher"
    assert info["CFBundleExecutable"] == "paperqueue"
    assert info["CFBundleName"] == info["CFBundleDisplayName"] == "paperqueue"
    assert info["CFBundleIconFile"] == "paperqueue"
    assert info["CFBundlePackageType"] == "APPL"
    assert info["LSUIElement"] is True
    assert info["CFBundleShortVersionString"] == __version__
    exe = app / "Contents" / "MacOS" / "paperqueue"
    script = exe.read_text()
    assert script.startswith("#!/bin/sh\n")
    assert "export PAPERQUEUE_HOME='/tmp/a dir/pq'" in script
    assert script.rstrip().endswith("-m paperqueue open")
    assert os.access(exe, os.X_OK)
    icns = app / "Contents" / "Resources" / "paperqueue.icns"
    assert icns.read_bytes()[:4] == b"icns"
    link = user / "Desktop" / "paperqueue"
    assert link.is_symlink() and os.readlink(link) == str(app)
    assert shortcut.app_state() == "ok" and shortcut.link_state() == "ok"


def test_second_install_is_a_no_op(user):
    shortcut.install_shortcut()
    rows = shortcut.install_shortcut()
    assert [r.status for r in rows] == ["ok", "ok"]


def test_stale_launcher_is_regenerated(user, monkeypatch):
    shortcut.install_shortcut(desktop=False)
    monkeypatch.setattr(shortcut.sys, "executable", "/moved/python3")
    assert shortcut.app_state() == "stale"
    rows = shortcut.install_shortcut(desktop=False)
    assert rows[0].status == "done" and "updated" in rows[0].detail
    assert "exec /moved/python3 -m paperqueue open" in (_app(user) / "Contents/MacOS/paperqueue").read_text()
    assert rows[1].status == "skipped"
    assert not (user / "Desktop").exists()


def test_refuses_foreign_bundle(user):
    app = _app(user)
    (app / "Contents").mkdir(parents=True)
    with (app / "Contents" / "Info.plist").open("wb") as f:
        plistlib.dump({"CFBundleIdentifier": "com.example.other"}, f)
    rows = shortcut.install_shortcut()
    assert rows[0].status == "needs you" and "left alone" in rows[0].detail
    assert len(rows) == 1
    assert not (app / "Contents" / "MacOS").exists()
    assert shortcut.remove_shortcut() == [f"Left {app} alone: it is not paperqueue's launcher"]
    assert app.is_dir()


def test_refuses_foreign_desktop_item(user, tmp_path):
    desktop = user / "Desktop"
    desktop.mkdir()
    other = tmp_path / "elsewhere.app"
    other.mkdir()
    (desktop / "paperqueue").symlink_to(other)
    rows = shortcut.install_shortcut()
    assert rows[1].status == "needs you"
    assert os.readlink(desktop / "paperqueue") == str(other)
    lines = shortcut.remove_shortcut()
    assert any("Left" in line and "Desktop" in line for line in lines)
    assert (desktop / "paperqueue").is_symlink()
    assert not _app(user).exists()  # our app was removed


def test_desktop_write_failure_is_reported(user):
    desktop = user / "Desktop"
    desktop.mkdir()
    desktop.chmod(0o500)
    try:
        rows = shortcut.install_shortcut()
    finally:
        desktop.chmod(0o700)
    assert rows[0].status == "done"
    assert rows[1].status == "needs you" and "Files and Folders" in rows[1].detail


def test_remove_dry_run_changes_nothing(user):
    shortcut.install_shortcut()
    lines = shortcut.remove_shortcut(dry_run=True)
    assert all(line.startswith("Would remove") for line in lines) and len(lines) == 2
    assert _app(user).is_dir() and (user / "Desktop" / "paperqueue").is_symlink()


# --- open ------------------------------------------------------------------------------------------


def test_open_when_running_does_not_kickstart(user, monkeypatch, tmp_path):
    calls = Calls()
    monkeypatch.setattr(subprocess, "run", calls)
    plist = tmp_path / "svc.plist"
    assert shortcut.open_app(8765, plist, "io.github.paperqueue", probe=lambda port: True, sleep=lambda s: None) == 0
    assert calls.argv == [["open", "-na", "Google Chrome", "--args", "--app=http://localhost:8765/"]]


def test_open_kickstarts_when_unreachable(user, monkeypatch, tmp_path):
    calls = Calls()
    monkeypatch.setattr(subprocess, "run", calls)
    plist = tmp_path / "svc.plist"
    plist.write_text("x")
    answers = iter([False, False, True])
    assert shortcut.open_app(8765, plist, "io.github.paperqueue", probe=lambda port: next(answers),
                             sleep=lambda s: None) == 0
    assert calls.argv[0] == ["launchctl", "kickstart", f"gui/{os.getuid()}/io.github.paperqueue"]
    assert calls.starts("open", "-na", "Google Chrome")
    assert not calls.starts("launchctl", "bootstrap")


def test_open_without_service_or_chrome(user, monkeypatch, tmp_path, capsys):
    calls = Calls({("open", "-na"): 1})
    monkeypatch.setattr(subprocess, "run", calls)
    plist = tmp_path / "missing.plist"
    assert shortcut.open_app(8765, plist, "l", probe=lambda port: False, sleep=lambda s: None) == 1
    assert calls.argv == [] and "install-service" in capsys.readouterr().err
    assert shortcut.open_app(9000, plist, "l", probe=lambda port: True, sleep=lambda s: None) == 0
    assert calls.argv[-1] == ["open", "http://localhost:9000/"]  # no Chrome: default browser


def test_open_gives_up_after_wait(user, monkeypatch, tmp_path, capsys):
    calls = Calls({("launchctl", "kickstart"): 113})
    monkeypatch.setattr(subprocess, "run", calls)
    plist = tmp_path / "svc.plist"
    plist.write_text("x")
    assert shortcut.open_app(8765, plist, "l", probe=lambda port: False, sleep=lambda s: None) == 1
    assert calls.starts("launchctl", "bootstrap")  # not loaded: bootstrapped instead
    assert not calls.starts("open")
    assert "did not start" in capsys.readouterr().err


# --- CLI -------------------------------------------------------------------------------------------


def test_cli_install_shortcut_and_uninstall(user, monkeypatch, capsys):
    assert cli.main(["install-shortcut", "--no-desktop"]) == 0
    assert _app(user).is_dir() and not (user / "Desktop").exists()
    skill = user / ".claude" / "skills" / "paper-queue" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text(SKILL_MD)
    plist = cli.plist_path()
    plist.parent.mkdir(parents=True)
    plist.write_text("<plist/>")
    capsys.readouterr()

    assert cli.main(["uninstall", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "Would run: launchctl bootout" in out and f"Would remove {skill}" in out
    assert _app(user).is_dir() and skill.exists() and plist.exists()

    calls = Calls()
    monkeypatch.setattr(subprocess, "run", calls)
    assert cli.main(["uninstall"]) == 0
    out = capsys.readouterr().out
    assert calls.argv == [["launchctl", "bootout", f"gui/{os.getuid()}/io.github.paperqueue"]]
    assert not _app(user).exists() and not skill.exists() and not plist.exists()
    assert "Kept your data" in out


def test_uninstall_keeps_foreign_skill(user, capsys):
    skill = user / ".claude" / "skills" / "paper-queue" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("my own notes")
    assert cli.main(["uninstall"]) == 0
    assert skill.read_text() == "my own notes"
    assert "not paperqueue's skill" in capsys.readouterr().out


def test_doctor_reports_stale_launcher(user, monkeypatch, capsys):
    assert cli.main(["doctor"]) == 0
    assert "[info] Shortcut: no launcher app" in capsys.readouterr().out
    shortcut.install_shortcut()
    monkeypatch.setattr(shortcut.sys, "executable", "/moved/python3")
    cli.main(["doctor"])
    out = capsys.readouterr().out
    assert "[warn] Shortcut" in out and "fix: paperqueue install-shortcut" in out


def test_parser_has_new_commands():
    p = cli.build_parser()
    assert p.parse_args(["setup", "--yes", "--no-zotero"]).no_zotero
    assert p.parse_args(["install-shortcut", "--no-desktop"]).no_desktop
    assert p.parse_args(["uninstall", "--dry-run"]).dry_run
    assert p.parse_args(["open"]).func is cli.cmd_open
