"""`paperqueue setup`: every step against a fake home, fake Zotero and a fake subprocess.run."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import httpx
import pytest

from paperqueue import cli, onboarding
from paperqueue.libraries import zotero as zotero_mod
from paperqueue.onboarding import LOCAL_API_LINE, merge_user_js, run_setup

CONFIG = (
    '[server]\ntoken = "t"\n'
    '# keep this comment\n[library]\nprovider = "off"\n'
    '[calendar]\nbackend = "off"\n[pdf]\nenabled = false\n'
)
PROFILES_INI = "[General]\nStartWithLastProfile=1\n\n[Profile0]\nName=default\nIsRelative=1\nPath=Profiles/ab12.default\nDefault=1\n"


class World:
    """A fake Mac: which(), subprocess.run() and the Zotero local API, all scriptable."""

    def __init__(self, home: Path, sysapps: Path):
        self.home = home
        self.sysapps = sysapps
        self.tools = {"brew": "/opt/homebrew/bin/brew", "claude": "/usr/local/bin/claude"}
        self.argv: list[list[str]] = []
        self.api = "down"  # enabled, disabled, down
        self.collection = True
        self.zotero_running = False
        self.loaded = False
        self.zotero_creates_profile = True
        self.ignores_user_js = False

    # collaborators
    def which(self, name: str) -> str | None:
        return self.tools.get(name)

    def run(self, argv, *a, **k):
        argv = [str(x) for x in argv]
        self.argv.append(argv)
        rc = 0
        if argv[1:4] == ["install", "--cask", "zotero"]:
            (self.sysapps / "Zotero.app").mkdir(parents=True)
        elif argv[:4] == ["open", "-g", "-a", "Zotero"]:
            self.zotero_running = True
            data = self.home / "Library" / "Application Support" / "Zotero"
            if self.zotero_creates_profile and not (data / "profiles.ini").exists():
                (data / "Profiles" / "ab12.default").mkdir(parents=True)
                (data / "profiles.ini").write_text(PROFILES_INI)
            elif LOCAL_API_LINE in self.user_js():
                self.api = "disabled" if self.ignores_user_js else "enabled"
        elif argv[:2] == ["pgrep", "-xi"]:
            rc = 0 if self.zotero_running else 1
        elif argv[:2] == ["launchctl", "print"]:
            rc = 0 if self.loaded else 113
        elif argv[:2] == ["launchctl", "bootstrap"]:
            self.loaded = True
        elif argv[1:3] == ["mcp", "add"]:
            (self.home / ".claude.json").write_text(json.dumps({"mcpServers": {"paperqueue": {}}}))
        return subprocess.CompletedProcess(argv, rc, "", "")

    def check(self, provider):
        if self.api == "down":
            return False, zotero_mod.NOT_RUNNING
        if self.api == "disabled":
            return False, zotero_mod.LOCAL_DISABLED
        if not self.collection:
            return False, "Connected to Zotero (local API), but there is no collection named 'To read'. Create it"
        return True, "Connected to Zotero (local API), collection 'To read', read-only."

    # helpers
    def profile(self) -> Path:
        return self.home / "Library" / "Application Support" / "Zotero" / "Profiles" / "ab12.default"

    def user_js(self) -> str:
        p = self.profile() / "user.js"
        return p.read_text() if p.exists() else ""

    def ran(self, *prefix: str) -> bool:
        return any(tuple(a[: len(prefix)]) == prefix for a in self.argv)

    def add_zotero(self, profile: bool = True) -> None:
        (self.sysapps / "Zotero.app").mkdir(parents=True, exist_ok=True)
        if profile:
            self.profile().mkdir(parents=True)
            (self.profile().parent.parent / "profiles.ini").write_text(PROFILES_INI)


@pytest.fixture
def world(tmp_path, monkeypatch) -> World:
    home = tmp_path / "user"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setenv("HOME", str(home))
    pq = tmp_path / "pqhome"
    pq.mkdir()
    (pq / "config.toml").write_text(CONFIG)
    monkeypatch.setenv("PAPERQUEUE_HOME", str(pq))
    monkeypatch.setattr(sys, "platform", "darwin")

    def no_network(self, request):
        raise httpx.ConnectError("network disabled in tests")

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", no_network)
    w = World(home, tmp_path / "Applications")
    monkeypatch.setattr(onboarding, "SYSTEM_APPLICATIONS", w.sysapps)
    monkeypatch.setattr(subprocess, "run", w.run)
    monkeypatch.setattr(zotero_mod.ZoteroProvider, "check", lambda self: w.check(self))
    return w


def setup(world: World, **kw) -> int:
    kw.setdefault("yes", True)
    return run_setup(which=world.which, sleep=lambda s: None, **kw)


def config_text() -> str:
    return (Path(__import__("os").environ["PAPERQUEUE_HOME"]) / "config.toml").read_text()


def statuses(out: str) -> dict[str, str]:
    """name -> status from the summary table."""
    table = out.split("\nSummary\n", 1)[1].split("\n\n", 1)[0]
    rows = {}
    for line in table.splitlines():
        name, _, status = line.strip().rpartition("  ")
        rows[name.strip()] = status.strip()
    return rows


# --- Zotero install ---------------------------------------------------------------------------------


def test_brew_installs_zotero_then_first_launch(world, capsys):
    assert setup(world) == 0
    out = capsys.readouterr().out
    assert world.ran("/opt/homebrew/bin/brew", "install", "--cask", "zotero")
    assert world.ran("open", "-g", "-a", "Zotero")  # first launch creates the profile
    rows = statuses(out)
    assert rows["Zotero"] == "done"
    assert rows["Zotero local API"] == "needs you"
    assert "Quit Zotero" in out and "Left for you:" in out
    assert world.user_js() == ""  # Zotero is running now: nothing written
    assert 'provider = "zotero"' in config_text() and "# keep this comment" in config_text()


def test_without_brew_points_to_download(world, capsys):
    world.tools.pop("brew")
    assert setup(world) == 0
    out = capsys.readouterr().out
    assert "https://www.zotero.org/download/" in out
    assert not world.ran("/opt/homebrew/bin/brew")
    assert statuses(out)["Library provider"] == "skipped"
    assert 'provider = "off"' in config_text()


def test_brew_declined(world, capsys):
    assert setup(world, yes=False, interactive=True, ask=lambda q: "n") == 0
    out = capsys.readouterr().out
    assert not world.ran("/opt/homebrew/bin/brew")
    assert statuses(out)["Zotero"] == "skipped"


def test_no_zotero_flag(world, capsys):
    assert setup(world, zotero=False, shortcut=False) == 0
    out = capsys.readouterr().out
    assert statuses(out)["Zotero"] == "skipped"
    assert "Zotero is not installed" not in out
    assert not world.ran("/opt/homebrew/bin/brew") and not world.ran("open")


# --- local API ---------------------------------------------------------------------------------------


def test_user_js_merge_is_idempotent(tmp_path):
    profile = tmp_path / "p"
    profile.mkdir()
    (profile / "prefs.js").write_text('user_pref("x", 1);\n')
    (profile / "user.js").write_text(
        'user_pref("browser.foo", 2);\nuser_pref("extensions.zotero.httpServer.localAPI.enabled", false);\n'
    )
    assert merge_user_js(profile) is True
    first = (profile / "user.js").read_text()
    assert first == f'user_pref("browser.foo", 2);\n{LOCAL_API_LINE}\n'
    assert merge_user_js(profile) is False
    assert (profile / "user.js").read_text() == first
    assert (profile / "prefs.js").read_text() == 'user_pref("x", 1);\n'
    fresh = tmp_path / "q"
    fresh.mkdir()
    assert merge_user_js(fresh) is True and (fresh / "user.js").read_text() == LOCAL_API_LINE + "\n"


def test_profile_present_enables_api(world, capsys):
    world.add_zotero()
    assert setup(world) == 0
    out = capsys.readouterr().out
    assert LOCAL_API_LINE in world.user_js()
    assert not (world.profile() / "prefs.js").exists()
    rows = statuses(out)
    assert rows["Zotero local API"] == "done" and rows["Zotero collection"] == "ok"


def test_user_js_not_taking_effect_asks_for_the_setting(world, capsys):
    world.add_zotero()
    world.ignores_user_js = True
    assert setup(world) == 0
    out = capsys.readouterr().out
    assert LOCAL_API_LINE in world.user_js()
    assert statuses(out)["Zotero local API"] == "needs you"
    assert "Allow other applications on this computer" in out and "did not answer" not in out


@pytest.mark.parametrize("api, running", [("down", True), ("disabled", True), ("disabled", False)])
def test_zotero_running_means_no_write(world, capsys, api, running):
    world.add_zotero()
    world.api, world.zotero_running = api, running
    assert setup(world) == 0
    out = capsys.readouterr().out
    assert world.user_js() == ""
    assert not world.ran("open", "-g", "-a", "Zotero")
    assert not any(a[0] in ("kill", "killall", "pkill", "osascript") for a in world.argv)
    assert statuses(out)["Zotero local API"] == "needs you"
    assert "Allow other applications on this computer" in out


def test_no_profile_and_zotero_never_creates_one(world, capsys):
    world.add_zotero(profile=False)
    world.zotero_creates_profile = False
    assert setup(world) == 0
    out = capsys.readouterr().out
    assert world.ran("open", "-g", "-a", "Zotero")
    assert "did not create its profile" in out


def test_missing_collection(world, capsys):
    world.add_zotero()
    world.api, world.collection = "enabled", False
    assert setup(world) == 0
    out = capsys.readouterr().out
    assert statuses(out)["Zotero collection"] == "needs you"
    assert "File > New Collection" in out and "To read" in out


# --- provider ----------------------------------------------------------------------------------------


@pytest.mark.parametrize("current", ["mendeley", "zotero"])
def test_provider_only_switched_from_off(world, capsys, current):
    world.add_zotero()
    world.api = "enabled"
    path = Path(__import__("os").environ["PAPERQUEUE_HOME"]) / "config.toml"
    path.write_text(CONFIG.replace('provider = "off"', f'provider = "{current}"'))
    assert setup(world) == 0
    out = capsys.readouterr().out
    assert f'provider = "{current}"' in config_text()
    assert statuses(out)["Library provider"] == ("skipped" if current == "mendeley" else "ok")


def test_provider_env_override_is_reported(world, monkeypatch, capsys):
    world.add_zotero()
    world.api = "enabled"
    monkeypatch.setenv("PAPERQUEUE_LIBRARY", "off")
    assert setup(world) == 0
    out = capsys.readouterr().out
    assert 'provider = "zotero"' in config_text()
    assert "PAPERQUEUE_LIBRARY=off" in out and statuses(out)["Library provider"] == "needs you"


# --- non-TTY, idempotence ----------------------------------------------------------------------------


def test_non_tty_without_yes_only_reports(world, capsys):
    world.add_zotero()
    before = config_text()
    assert setup(world, yes=False, interactive=False) == 2
    out = capsys.readouterr().out
    assert "only reports" in out and "Nothing was changed" in out
    assert config_text() == before
    assert world.user_js() == ""
    assert not (world.home / "Applications").exists() and not (world.home / "Desktop").exists()
    assert not (world.home / ".claude").exists()
    assert not cli.plist_path().exists()
    mutating = [a for a in world.argv if a[0] not in ("pgrep",) and a[:2] != ["launchctl", "print"]]
    assert mutating == []


def test_full_run_then_second_run_is_idempotent(world, capsys):
    world.add_zotero()
    world.api = "enabled"
    assert setup(world) == 0
    first = statuses(capsys.readouterr().out)
    assert first["Background service"] == "done"
    assert first["Claude Code MCP"] == "done" and first["Claude Code skill"] == "done"
    assert first["Launcher app"] == "done" and first["Desktop shortcut"] == "done"
    assert first["Library provider"] == "done"
    assert (world.home / "Desktop" / "paperqueue").is_symlink()
    mcp = next(a for a in world.argv if a[1:3] == ["mcp", "add"])
    assert mcp[3:7] == ["-s", "user", "paperqueue", "--"] and mcp[-1] == "mcp"

    world.argv.clear()
    assert setup(world) == 0
    out = capsys.readouterr().out
    second = statuses(out)
    assert set(second.values()) <= {"ok", "skipped"}, second
    assert "All set" in out
    assert not world.ran("launchctl", "bootstrap") and not world.ran("/usr/local/bin/claude")


def test_provider_change_restarts_loaded_service(world, capsys):
    world.add_zotero()
    world.api = "enabled"
    assert setup(world, zotero=False) == 0  # service installed, provider still off
    assert 'provider = "off"' in config_text()
    capsys.readouterr()
    world.argv.clear()
    assert setup(world) == 0
    assert world.ran("launchctl", "kickstart", "-k")
    assert statuses(capsys.readouterr().out)["Background service"] == "done"


def test_cli_setup_flags(world, monkeypatch, capsys):
    seen = {}
    monkeypatch.setattr(onboarding, "run_setup", lambda **kw: seen.update(kw) or 0)
    assert cli.main(["setup", "--yes", "--no-shortcut"]) == 0
    assert seen == {"yes": True, "zotero": True, "shortcut": False}
