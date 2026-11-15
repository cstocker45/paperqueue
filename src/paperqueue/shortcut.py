"""The macOS launcher: ~/Applications/paperqueue.app, a Desktop symlink to it, and `paperqueue open`.

The app bundle is a shell script that runs `python -m paperqueue open`, which makes sure the background
service is up and then opens the web app in a Chrome app window. It is written locally, so it carries no
quarantine attribute and needs no signing. paperqueue only ever changes or removes a bundle whose
CFBundleIdentifier is its own, and a Desktop item that is a symlink to that bundle.
"""

from __future__ import annotations

import os
import plistlib
import shlex
import shutil
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from . import __version__

APP_NAME = "paperqueue"
BUNDLE_ID = "io.github.paperqueue.launcher"
ICON_SOURCE = Path(__file__).parent / "assets" / "paperqueue.icns"
OPEN_WAIT_SECONDS = 10.0

OK, DONE, SKIPPED, NEEDS_YOU, PENDING = "ok", "done", "skipped", "needs you", "pending"


@dataclass
class Outcome:
    """One line of a setup-style report. `status` is ok, done, skipped, needs you or pending."""

    name: str
    status: str
    detail: str = ""


# --- locations -------------------------------------------------------------------------------------


def apps_dir() -> Path:
    return Path.home() / "Applications"


def desktop_dir() -> Path:
    return Path.home() / "Desktop"


def app_path(apps: Path | None = None) -> Path:
    return (apps or apps_dir()) / f"{APP_NAME}.app"


def desktop_link(desktop: Path | None = None) -> Path:
    return (desktop or desktop_dir()) / APP_NAME


# --- bundle contents -------------------------------------------------------------------------------


def info_plist() -> dict[str, Any]:
    return {
        "CFBundleIdentifier": BUNDLE_ID,
        "CFBundleExecutable": APP_NAME,
        "CFBundleName": APP_NAME,
        "CFBundleDisplayName": APP_NAME,
        "CFBundleIconFile": APP_NAME,
        "CFBundlePackageType": "APPL",
        "CFBundleInfoDictionaryVersion": "6.0",
        "CFBundleShortVersionString": __version__,
        "CFBundleVersion": __version__,
        "LSUIElement": True,
        "LSMinimumSystemVersion": "11.0",
    }


def launcher_script(executable: str | None = None, home: str | None = None) -> str:
    """The bundle's executable. Exports PAPERQUEUE_HOME when it is set, as the launchd plist does."""
    executable = executable or sys.executable
    home = os.environ.get("PAPERQUEUE_HOME") if home is None else home
    lines = ["#!/bin/sh", "# paperqueue launcher, written by `paperqueue install-shortcut`."]
    if home:
        lines.append(f"export PAPERQUEUE_HOME={shlex.quote(home)}")
    lines.append(f"exec {shlex.quote(executable)} -m paperqueue open")
    return "\n".join(lines) + "\n"


def bundle_id(app: Path) -> str | None:
    try:
        with (app / "Contents" / "Info.plist").open("rb") as f:
            value = plistlib.load(f).get("CFBundleIdentifier")
    except (OSError, plistlib.InvalidFileException, ValueError, AttributeError):
        return None
    return value if isinstance(value, str) else None


def is_ours(app: Path) -> bool:
    return not app.is_symlink() and app.is_dir() and bundle_id(app) == BUNDLE_ID


def app_state(app: Path | None = None) -> str:
    """missing, ok, stale (ours but out of date, e.g. the Python it runs moved) or foreign."""
    app = app or app_path()
    if not app.exists() and not app.is_symlink():
        return "missing"
    if not is_ours(app):
        return "foreign"
    contents = app / "Contents"
    try:
        script = (contents / "MacOS" / APP_NAME).read_text(encoding="utf-8")
        with (contents / "Info.plist").open("rb") as f:
            plist = plistlib.load(f)
    except (OSError, plistlib.InvalidFileException, ValueError):
        return "stale"
    fresh = (
        script == launcher_script()
        and plist == info_plist()
        and os.access(contents / "MacOS" / APP_NAME, os.X_OK)
        and (contents / "Resources" / f"{APP_NAME}.icns").is_file()
    )
    return "ok" if fresh else "stale"


def link_state(link: Path | None = None, app: Path | None = None) -> str:
    """missing, ok (a symlink to our app) or foreign (anything else at that path)."""
    link, app = link or desktop_link(), app or app_path()
    if link.is_symlink():
        return "ok" if os.readlink(link) == str(app) else "foreign"
    return "foreign" if link.exists() else "missing"


def _write_bundle(app: Path) -> None:
    contents = app / "Contents"
    (contents / "MacOS").mkdir(parents=True, exist_ok=True)
    (contents / "Resources").mkdir(parents=True, exist_ok=True)
    with (contents / "Info.plist").open("wb") as f:
        plistlib.dump(info_plist(), f)
    (contents / "PkgInfo").write_text("APPL????", encoding="ascii")
    exe = contents / "MacOS" / APP_NAME
    exe.write_text(launcher_script(), encoding="utf-8")
    exe.chmod(0o755)
    shutil.copyfile(ICON_SOURCE, contents / "Resources" / f"{APP_NAME}.icns")
    os.utime(app)  # Finder re-reads the icon of a bundle whose date changed


def install_shortcut(desktop: bool = True, apps: Path | None = None, desk: Path | None = None) -> list[Outcome]:
    """Create or refresh the launcher app, then the Desktop symlink. Never touches a foreign item."""
    app = app_path(apps)
    link = desktop_link(desk)
    out: list[Outcome] = []
    state = app_state(app)
    if state == "foreign":
        out.append(Outcome("Launcher app", NEEDS_YOU,
                           f"{app} exists and is not paperqueue's launcher, so it was left alone. "
                           "Move it elsewhere, then run `paperqueue install-shortcut`."))
        return out
    if state == "ok":
        out.append(Outcome("Launcher app", OK, str(app)))
    else:
        try:
            _write_bundle(app)
        except OSError as exc:
            out.append(Outcome("Launcher app", NEEDS_YOU, f"could not write {app}: {exc}"))
            return out
        out.append(Outcome("Launcher app", DONE, f"{'updated' if state == 'stale' else 'created'} {app}"))

    if not desktop:
        out.append(Outcome("Desktop shortcut", SKIPPED, "--no-desktop"))
        return out
    lstate = link_state(link, app)
    if lstate == "ok":
        out.append(Outcome("Desktop shortcut", OK, str(link)))
    elif lstate == "foreign":
        out.append(Outcome("Desktop shortcut", NEEDS_YOU,
                           f"{link} already exists and is not a link to {app}; left alone. "
                           "Drag the app from ~/Applications to the Desktop with Cmd-Option held instead."))
    else:
        try:
            link.parent.mkdir(parents=True, exist_ok=True)
            link.symlink_to(app)
        except OSError as exc:
            out.append(Outcome("Desktop shortcut", NEEDS_YOU,
                               f"could not write to the Desktop ({exc.strerror or exc}). Allow your terminal "
                               "in System Settings > Privacy & Security > Files and Folders, then re-run, "
                               f"or run: ln -s {shlex.quote(str(app))} {shlex.quote(str(link))}"))
        else:
            out.append(Outcome("Desktop shortcut", DONE, f"{link} -> {app}"))
    return out


def remove_shortcut(dry_run: bool = False, apps: Path | None = None, desk: Path | None = None) -> list[str]:
    """Remove the Desktop symlink and the launcher app if they are ours. Returns what happened."""
    app, link = app_path(apps), desktop_link(desk)
    verb = "Would remove" if dry_run else "Removed"
    lines: list[str] = []
    lstate = link_state(link, app)
    if lstate == "ok":
        if not dry_run:
            link.unlink()
        lines.append(f"{verb} {link}")
    elif lstate == "foreign":
        lines.append(f"Left {link} alone: it is not paperqueue's shortcut")
    if is_ours(app):
        if not dry_run:
            shutil.rmtree(app)
        lines.append(f"{verb} {app}")
    elif app.exists() or app.is_symlink():
        lines.append(f"Left {app} alone: it is not paperqueue's launcher")
    return lines


# --- paperqueue open -------------------------------------------------------------------------------


def server_reachable(port: int) -> bool:
    try:
        r = httpx.get(f"http://127.0.0.1:{port}/api/status", timeout=1.5)
    except httpx.HTTPError:
        return False
    return r.status_code == 200


def open_app(
    port: int,
    plist: Path,
    label: str,
    *,
    probe: Callable[[int], bool] = server_reachable,
    sleep: Callable[[float], None] = time.sleep,
    wait: float = OPEN_WAIT_SECONDS,
) -> int:
    """Start the service if it is down, then open the web app in a Chrome app window."""
    url = f"http://localhost:{port}/"
    if not probe(port):
        if not plist.exists():
            print("paperqueue is not running and its background service is not installed.\n"
                  "Run `paperqueue install-service` (or `paperqueue setup`), or `paperqueue serve`.",
                  file=sys.stderr)
            return 1
        domain = f"gui/{os.getuid()}"
        proc = subprocess.run(["launchctl", "kickstart", f"{domain}/{label}"], check=False, capture_output=True)
        if proc.returncode != 0:  # not loaded (e.g. booted out): load it, which also starts it
            subprocess.run(["launchctl", "bootstrap", domain, str(plist)], check=False, capture_output=True)
        step = 0.5
        for _ in range(max(1, int(wait / step))):
            sleep(step)
            if probe(port):
                break
        else:
            print(f"paperqueue did not start within {wait:.0f} s. See the logs in ~/.paperqueue/logs/ "
                  "or run `paperqueue doctor`.", file=sys.stderr)
            return 1
    chrome = subprocess.run(["open", "-na", "Google Chrome", "--args", f"--app={url}"],
                            check=False, capture_output=True)
    if chrome.returncode != 0:
        subprocess.run(["open", url], check=False)
    return 0
