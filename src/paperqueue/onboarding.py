"""`paperqueue setup`: install and connect everything paperqueue can use, step by step.

Each step reports ok, done, skipped or needs you, and re-running the command resumes where it stopped.
Setup never uses sudo, never installs Homebrew, never quits or kills an app, never edits Zotero's prefs.js
(only user.js, one line), never scripts Finder or System Events, never overwrites a library provider other
than "off", and never deletes user data.
"""

from __future__ import annotations

import configparser
import os
import platform
import re
import shutil
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from .config import Config
from .shortcut import DONE, NEEDS_YOU, OK, PENDING, SKIPPED, Outcome

SYSTEM_APPLICATIONS = Path("/Applications")
ZOTERO_DOWNLOAD_URL = "https://www.zotero.org/download/"
ZOTERO_CASK = "zotero"
LOCAL_API_PREF = "extensions.zotero.httpServer.localAPI.enabled"
LOCAL_API_LINE = f'user_pref("{LOCAL_API_PREF}", true);'
LOCAL_API_SETTING = (
    'In Zotero, open Settings > Advanced and turn on "Allow other applications on this computer to '
    'communicate with Zotero"'
)
PROFILE_WAIT_SECONDS = 60.0
API_WAIT_SECONDS = 30.0
_PREF_RE = re.compile(r'^\s*user_pref\(\s*"' + re.escape(LOCAL_API_PREF) + r'"\s*,')


@dataclass
class Setup:
    """Inputs and collaborators for one run; tests replace `which`, `confirm` and `sleep`."""

    cfg: Config
    apply: bool
    zotero: bool = True
    shortcut: bool = True
    which: Callable[[str], str | None] = shutil.which
    confirm: Callable[[str], bool] = lambda question: True
    sleep: Callable[[float], None] = time.sleep
    rows: list[Outcome] = field(default_factory=list)
    zotero_app: Path | None = None
    api_enabled: bool = False
    config_changed: bool = False

    @property
    def no_zotero_reason(self) -> str:
        return "--no-zotero" if not self.zotero else "Zotero is not installed"

    def report(self, name: str, status: str, detail: str = "") -> Outcome:
        row = Outcome(name, status, detail)
        self.rows.append(row)
        print(f"[{status}] {name}" + (f": {detail}" if detail else ""), flush=True)
        return row


# --- Zotero helpers --------------------------------------------------------------------------------


def zotero_data_dir() -> Path:
    return Path.home() / "Library" / "Application Support" / "Zotero"


def find_zotero_app() -> Path | None:
    for base in (SYSTEM_APPLICATIONS, Path.home() / "Applications"):
        app = base / "Zotero.app"
        if app.is_dir():
            return app
    return None


def find_profile(data_dir: Path | None = None) -> Path | None:
    """The default Zotero profile directory listed in profiles.ini, or None."""
    data_dir = data_dir or zotero_data_dir()
    ini = configparser.ConfigParser(interpolation=None)
    try:
        if not ini.read(data_dir / "profiles.ini", encoding="utf-8"):
            return None
    except configparser.Error:
        return None
    profiles = [ini[s] for s in ini.sections() if s.startswith("Profile") and ini[s].get("Path")]
    if not profiles:
        return None
    chosen = next((p for p in profiles if p.get("Default") == "1"), profiles[0])
    rel = chosen.get("IsRelative", "1") == "1"
    path = data_dir / chosen["Path"] if rel else Path(chosen["Path"])
    return path if path.is_dir() else None


def merge_user_js(profile: Path) -> bool:
    """Make user.js turn the local API on. Returns False when it already does. Never touches prefs.js."""
    path = profile / "user.js"
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    lines = text.splitlines()
    if LOCAL_API_LINE in (line.strip() for line in lines):
        return False
    kept = [line for line in lines if not _PREF_RE.match(line)]
    kept.append(LOCAL_API_LINE)
    path.write_text("\n".join(kept) + "\n", encoding="utf-8")
    return True


def zotero_running() -> bool:
    proc = subprocess.run(["pgrep", "-xi", "zotero"], check=False, capture_output=True)
    return proc.returncode == 0


def probe_local_api(cfg) -> str:
    """enabled, disabled (Zotero up, local API off), down (nothing on port 23119) or error."""
    from .libraries.zotero import LOCAL_DISABLED, NOT_RUNNING, ZoteroProvider

    ok, msg = ZoteroProvider(cfg).check()
    if msg == NOT_RUNNING:
        return "down"
    if msg == LOCAL_DISABLED:
        return "disabled"
    if ok or "no collection named" in msg:
        return "enabled"
    return "error"


def _start_zotero() -> None:
    subprocess.run(["open", "-g", "-a", "Zotero"], check=False, capture_output=True)


# --- steps -----------------------------------------------------------------------------------------


def step_zotero_install(s: Setup) -> None:
    name = "Zotero"
    if not s.zotero:
        s.report(name, SKIPPED, "--no-zotero")
        return
    s.zotero_app = find_zotero_app()
    if s.zotero_app:
        s.report(name, OK, str(s.zotero_app))
        return
    brew = s.which("brew")
    if not brew:
        s.report(name, NEEDS_YOU, f"install Zotero 7 from {ZOTERO_DOWNLOAD_URL}, then re-run `paperqueue setup`")
        return
    if not s.apply:
        s.report(name, PENDING, f"would run: brew install --cask {ZOTERO_CASK}")
        return
    if not s.confirm(f"Zotero is not installed. Install it with Homebrew (brew install --cask {ZOTERO_CASK})?"):
        s.report(name, SKIPPED, f"not installed; you can get it from {ZOTERO_DOWNLOAD_URL}")
        return
    print(f"Running: brew install --cask {ZOTERO_CASK}", flush=True)
    proc = subprocess.run([brew, "install", "--cask", ZOTERO_CASK], check=False)
    s.zotero_app = find_zotero_app()
    if proc.returncode != 0 or not s.zotero_app:
        s.report(name, NEEDS_YOU, f"Homebrew could not install Zotero (exit {proc.returncode}); "
                                  f"install it from {ZOTERO_DOWNLOAD_URL}, then re-run `paperqueue setup`")
        return
    s.report(name, DONE, f"installed {s.zotero_app}")


def step_local_api(s: Setup) -> None:
    name = "Zotero local API"
    if not s.zotero_app:
        s.report(name, SKIPPED, s.no_zotero_reason)
        return
    if getattr(s.cfg.zotero, "source", "local") != "local":
        s.report(name, SKIPPED, "zotero.source is 'web'")
        return
    state = probe_local_api(s.cfg)
    if state == "enabled":
        s.api_enabled = True
        s.report(name, OK, "enabled")
        return
    if state != "down" or zotero_running():  # Zotero is up: never write its prefs or quit it
        s.report(name, NEEDS_YOU, f"{LOCAL_API_SETTING}, then re-run `paperqueue setup`")
        return
    profile = find_profile()
    if profile is None:
        if not s.apply:
            s.report(name, PENDING, "would start Zotero once so that it creates its profile")
            return
        print("Starting Zotero once so that it creates its profile...", flush=True)
        _start_zotero()
        ini = zotero_data_dir() / "profiles.ini"
        waited = 0.0
        while not ini.exists() and waited < PROFILE_WAIT_SECONDS:
            s.sleep(1.0)
            waited += 1.0
        if ini.exists():
            s.report(name, NEEDS_YOU, "Zotero has created its profile. Quit Zotero (Zotero > Quit Zotero), then "
                                      "re-run `paperqueue setup` to turn on its local API")
        else:
            s.report(name, NEEDS_YOU, "Zotero did not create its profile within a minute. Open Zotero once, "
                                      "quit it, then re-run `paperqueue setup`")
        return
    if not s.apply:
        s.report(name, PENDING, f"would add the local API setting to {profile / 'user.js'}")
        return
    wrote = merge_user_js(profile)
    _start_zotero()
    waited = 0.0
    while waited < API_WAIT_SECONDS:
        s.sleep(1.0)
        waited += 1.0
        state = probe_local_api(s.cfg)
        if state == "enabled":
            s.api_enabled = True
            if wrote:
                s.report(name, DONE, f"turned on in {profile / 'user.js'} and started Zotero")
            else:
                s.report(name, OK, "enabled (Zotero was closed, so setup started it)")
            return
        if state != "down":  # Zotero is up but did not take the setting
            s.report(name, NEEDS_YOU, f"{LOCAL_API_SETTING}, then re-run `paperqueue setup`")
            return
    s.report(name, NEEDS_YOU, f"turned on in {profile / 'user.js'}, but Zotero did not answer within "
                              f"{API_WAIT_SECONDS:.0f} s. Start Zotero, then re-run `paperqueue setup`")


def step_provider(s: Setup) -> None:
    from .config import file_library_provider, set_library_provider

    name = "Library provider"
    path = s.cfg.config_path
    current = file_library_provider(path)
    env = os.environ.get("PAPERQUEUE_LIBRARY", "").strip().lower()
    override = f"; note that PAPERQUEUE_LIBRARY={env} in your environment overrides it" if env and env != "zotero" else ""
    if current == "zotero":
        s.report(name, NEEDS_YOU if override else OK, "zotero" + override)
        return
    if current != "off":
        s.report(name, SKIPPED, f"set to '{current}'; left as it is")
        return
    if not s.zotero_app:
        s.report(name, SKIPPED, f"off ({s.no_zotero_reason})")
        return
    if not s.apply:
        s.report(name, PENDING, f"would set [library] provider = \"zotero\" in {path}")
        return
    set_library_provider(path, "zotero")
    s.config_changed = True
    s.report(name, NEEDS_YOU if override else DONE, f"set to zotero in {path}" + override)


def step_collection(s: Setup) -> None:
    from .libraries.zotero import ZoteroProvider

    name = "Zotero collection"
    zcfg = s.cfg.zotero
    if not s.zotero_app:
        s.report(name, SKIPPED, s.no_zotero_reason)
        return
    if zcfg.source == "local" and not s.api_enabled:
        s.report(name, SKIPPED, "checked once the local API is on")
        return
    ok, msg = ZoteroProvider(s.cfg).check()
    if ok:
        s.report(name, OK, msg)
    elif "no collection named" in msg:
        s.report(name, NEEDS_YOU, f"in Zotero choose File > New Collection and name it '{zcfg.collection}' "
                                  "in My Library; paperqueue then reads and writes it via the local connector")
    else:
        s.report(name, NEEDS_YOU, msg)


def step_calendar(s: Setup) -> None:
    from .calendar import get_backend

    name = "Calendar"
    backend = get_backend(s.cfg)
    if backend is None:
        s.report(name, SKIPPED, "calendar sync is off")
        return
    print("Checking Apple Calendar (macOS may ask to allow Automation access to Calendar; click OK)...",
          flush=True)
    ok, msg = backend.check()
    if ok:
        s.report(name, OK, msg)
        return
    cal = s.cfg.calendar.apple_calendar
    s.report(name, NEEDS_YOU, f"in Calendar.app choose File > New Calendar > iCloud and name it '{cal}', and allow "
                              "Calendar in System Settings > Privacy & Security > Automation, then re-run setup "
                              f"({msg})")


def step_service(s: Setup) -> None:
    from . import cli

    name = "Background service"
    if not s.apply:
        state = cli.service_state(s.cfg)
        s.report(name, OK if state == "ok" else PENDING,
                 "installed and running" if state == "ok" else "would install the login service")
        return
    status, detail = cli.ensure_service(s.cfg, restart=s.config_changed)
    s.report(name, status, detail)


def paperqueue_command(which: Callable[[str], str | None]) -> list[str]:
    exe = which("paperqueue")
    return [exe] if exe else [sys.executable, "-m", "paperqueue"]


def step_mcp(s: Setup) -> None:
    from . import cli

    name = "Claude Code MCP"
    if cli._mcp_registered():
        s.report(name, OK, "registered")
        return
    claude = s.which("claude")
    if not claude:
        s.report(name, SKIPPED, "Claude Code is not installed")
        return
    argv = [claude, "mcp", "add", "-s", "user", "paperqueue", "--", *paperqueue_command(s.which), "mcp"]
    if not s.apply:
        s.report(name, PENDING, "would run: " + " ".join(argv))
        return
    proc = subprocess.run(argv, check=False, capture_output=True, text=True)
    if proc.returncode == 0:
        s.report(name, DONE, "registered for all your projects")
    else:
        s.report(name, NEEDS_YOU, f"`claude mcp add` failed; run: {cli.MCP_ADD_HINT}")


def step_skill(s: Setup) -> None:
    from .skill import SKILL_MD, default_skill_path, install_skill

    name = "Claude Code skill"
    path = default_skill_path()
    if not s.which("claude") and not path.parent.parent.parent.is_dir():
        s.report(name, SKIPPED, "Claude Code is not installed")
        return
    if path.is_file() and path.read_text(encoding="utf-8") == SKILL_MD:
        s.report(name, OK, str(path))
        return
    if not s.apply:
        s.report(name, PENDING, f"would write {path}")
        return
    s.report(name, DONE, str(install_skill()))


def step_shortcut(s: Setup) -> None:
    from . import shortcut

    if not s.shortcut:
        s.report("Launcher app", SKIPPED, "--no-shortcut")
        return
    if not s.apply:
        state, lstate = shortcut.app_state(), shortcut.link_state()
        s.report("Launcher app", OK if state == "ok" else PENDING if state != "foreign" else NEEDS_YOU,
                 {"ok": str(shortcut.app_path()), "missing": "would create ~/Applications/paperqueue.app",
                  "stale": "would update the launcher", "foreign": "a different paperqueue.app is in the way"}[state])
        s.report("Desktop shortcut", OK if lstate == "ok" else PENDING if lstate == "missing" else NEEDS_YOU,
                 {"ok": str(shortcut.desktop_link()), "missing": "would link ~/Desktop/paperqueue to the app",
                  "foreign": "~/Desktop/paperqueue already exists and is not ours"}[lstate])
        return
    for row in shortcut.install_shortcut():
        s.report(row.name, row.status, row.detail)


# --- driver ----------------------------------------------------------------------------------------


def _summary(rows: list[Outcome], apply: bool) -> None:
    width = max(len(r.name) for r in rows)
    print("\nSummary")
    for r in rows:
        print(f"  {r.name.ljust(width)}  {r.status}")
    todo = [r for r in rows if r.status == NEEDS_YOU]
    if todo:
        print("\nLeft for you:")
        for i, r in enumerate(todo, 1):
            print(f"  {i}. {r.name}: {r.detail}")
        print("\nThen run `paperqueue setup` again; finished steps are skipped.")
    elif apply:
        print("\nAll set. Open paperqueue from the Desktop, or run `paperqueue open`.")
    if not apply:
        print("\nNothing was changed. Run `paperqueue setup` in a terminal, or pass --yes, to apply.")


def run_setup(
    *,
    yes: bool = False,
    zotero: bool = True,
    shortcut: bool = True,
    interactive: bool | None = None,
    which: Callable[[str], str | None] = shutil.which,
    ask: Callable[[str], str] = input,
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    """Run every step. Returns 0 (also when manual steps remain), 1 on a failed preflight and 2 when it only
    reported because nobody can answer questions (no TTY and no --yes)."""
    from .config import load_config

    if interactive is None:
        interactive = sys.stdin.isatty()
    apply = yes or interactive

    def confirm(question: str) -> bool:
        if yes:
            return True
        try:
            answer = ask(f"{question} [Y/n] ").strip().lower()
        except EOFError:
            return False
        return answer in ("", "y", "yes")

    if sys.platform != "darwin":
        print("[needs you] macOS: paperqueue setup needs macOS. On other systems run `paperqueue serve`.")
        return 1
    try:
        cfg = load_config()
    except Exception as exc:  # noqa: BLE001
        print(f"[needs you] Config: {exc}\n  Fix or delete the config file (a default is recreated), then re-run.")
        return 1
    s = Setup(cfg=cfg, apply=apply, zotero=zotero, shortcut=shortcut, which=which, confirm=confirm, sleep=sleep)
    if not apply:
        print("No terminal to ask questions in, so this run only reports. Pass --yes to apply the defaults.\n")
    s.report("macOS", OK, platform.mac_ver()[0] or "unknown version")
    s.report("Config", OK, str(cfg.config_path))
    for step in (step_zotero_install, step_local_api, step_provider, step_collection, step_calendar,
                 step_service, step_mcp, step_skill, step_shortcut):
        step(s)
    _summary(s.rows, apply)
    return 0 if apply else 2
