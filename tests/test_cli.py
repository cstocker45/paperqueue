"""CLI parsing and commands via cli.main([...])."""

from __future__ import annotations

import json
import plistlib
import sys
import threading

import httpx
import pytest

from paperqueue import cli
from paperqueue.skill import SKILL_MD

ARXIV_FEED = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom" xmlns:arxiv="http://arxiv.org/schemas/atom">
  <entry>
    <id>http://arxiv.org/abs/1706.03762v1</id>
    <published>2017-06-12T17:57:34Z</published>
    <title>Attention Is All You Need</title>
    <summary>Transformers.</summary>
    <author><name>Ashish Vaswani</name></author>
  </entry>
</feed>"""

CONFIG = (
    '[server]\ntoken = "tok-SECRET-1"\n'
    '[library]\nprovider = "off"\n'
    '[zotero]\napi_key = "zot-SECRET-2"\nuser_id = "123"\n'
    '[mendeley]\nclient_id = "abc"\nclient_secret = "men-SECRET-3"\n'
    '[calendar]\nbackend = "off"\n'
    '[pdf]\nenabled = false\n'
)


def _handler(request: httpx.Request) -> httpx.Response:
    if request.url.host == "export.arxiv.org" and request.url.params.get("id_list") == "1706.03762":
        return httpx.Response(200, text=ARXIV_FEED)
    return httpx.Response(404)


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / "pqhome"
    h.mkdir()
    (h / "config.toml").write_text(CONFIG)
    monkeypatch.setenv("PAPERQUEUE_HOME", str(h))
    monkeypatch.setenv("PAPERQUEUE_LIBRARY", "off")
    monkeypatch.setenv("PAPERQUEUE_CALENDAR", "off")
    for var in ("ZOTERO_API_KEY", "ZOTERO_USER_ID", "MENDELEY_CLIENT_ID", "MENDELEY_CLIENT_SECRET", "PAPERQUEUE_TOKEN"):
        monkeypatch.delenv(var, raising=False)
    # never touch the real home directory, ~/.claude or ~/Library
    fake_user_home = tmp_path / "user"
    fake_user_home.mkdir()
    monkeypatch.setattr(cli.Path, "home", classmethod(lambda cls: fake_user_home))

    def no_network(self, request):
        raise httpx.ConnectError(f"network disabled in tests: {request.url}")

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", no_network)

    def forbid(*a, **k):
        raise AssertionError(f"subprocess called: {a}")

    monkeypatch.setattr(cli.subprocess, "run", forbid)
    return h


@pytest.fixture
def queue(home, monkeypatch):
    from paperqueue.config import load_config
    from paperqueue.metadata import Resolver
    from paperqueue.service import PaperQueue

    q = PaperQueue(load_config(home), resolver=Resolver(client=httpx.Client(transport=httpx.MockTransport(_handler))))
    monkeypatch.setattr(cli, "make_queue", lambda cfg=None: q)
    return q


def test_parser_commands():
    p = cli.build_parser()
    a = p.parse_args(["add", "1706.03762", "-t", "nlp", "-t", "ml", "--when", "tomorrow", "-p", "high"])
    assert (a.ref, a.tag, a.when, a.priority) == ("1706.03762", ["nlp", "ml"], "tomorrow", "high")
    assert p.parse_args(["serve", "--lan", "--port", "9000"]).port == 9000
    assert p.parse_args(["list", "read", "--json"]).view == "read"
    assert p.parse_args(["schedule", "3", "friday"]).when == "friday"
    assert p.parse_args(["install-service", "--uninstall", "--dry-run"]).uninstall
    assert p.parse_args(["mendeley", "login"]).func is cli.cmd_mendeley
    with pytest.raises(SystemExit):
        p.parse_args(["list", "bogus"])


def test_version(capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main(["--version"])
    assert exc.value.code == 0
    assert "paperqueue" in capsys.readouterr().out


def test_add_list_upcoming_read_remove(queue, capsys):
    assert cli.main(["add", "1706.03762", "-t", "nlp"]) == 0
    out = capsys.readouterr().out
    assert "Added #" in out and "Attention Is All You Need" in out and " for " in out

    assert cli.main(["add", "https://arxiv.org/abs/1706.03762"]) == 0
    assert "Already queued" in capsys.readouterr().out

    assert cli.main(["list", "all"]) == 0
    out = capsys.readouterr().out
    assert "Attention Is All You Need" in out and "Ashish Vaswani" in out and "#nlp" in out

    assert cli.main(["list", "all", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data[0]["title"] == "Attention Is All You Need"
    pid = data[0]["id"]

    assert cli.main(["upcoming", "--days", "14", "--json"]) == 0
    up = json.loads(capsys.readouterr().out)
    assert [p["id"] for p in up["sessions"]] == [pid]

    assert cli.main(["upcoming", "--days", "14"]) == 0
    out = capsys.readouterr().out
    assert f"#{pid} Attention" in out

    assert cli.main(["schedule", str(pid), "none"]) == 0
    assert "Unscheduled" in capsys.readouterr().out
    assert cli.main(["schedule", str(pid), "tomorrow"]) == 0
    assert "Scheduled" in capsys.readouterr().out

    assert cli.main(["read", str(pid)]) == 0
    assert "Marked read" in capsys.readouterr().out
    assert cli.main(["remove", str(pid)]) == 0
    assert "Removed" in capsys.readouterr().out
    assert cli.main(["list", "all"]) == 0
    assert "No papers" in capsys.readouterr().out


def test_unknown_id_is_an_error(queue, capsys):
    assert cli.main(["read", "4242"]) == 1
    assert "4242" in capsys.readouterr().err


def test_install_service_dry_run(home, capsys):
    assert cli.main(["install-service", "--dry-run"]) == 0
    out = capsys.readouterr().out
    xml = out[out.index("<?xml"): out.index("</plist>") + len("</plist>")]
    plist = plistlib.loads(xml.encode())
    assert plist["Label"] == "io.github.paperqueue"
    assert plist["ProgramArguments"] == [sys.executable, "-m", "paperqueue", "serve"]
    assert plist["RunAtLoad"] is True and plist["KeepAlive"] is True
    assert plist["StandardOutPath"].startswith(str(home / "logs"))
    assert "launchctl bootstrap gui/" in out
    assert not cli.plist_path().exists()

    assert cli.main(["install-service", "--uninstall", "--dry-run"]) == 0
    assert "bootout" in capsys.readouterr().out


def test_config_masks_secrets(home, capsys):
    assert cli.main(["config"]) == 0
    out = capsys.readouterr().out
    assert str(home / "config.toml") in out
    assert "SECRET" not in out
    assert out.count('"********"') == 3
    assert 'user_id = "123"' in out


def test_mask_secrets_keeps_empty_values():
    text = 'api_key = ""\ntoken = "abc"\nclient_id = "visible"\n'
    assert cli.mask_secrets(text) == 'api_key = ""\ntoken = "********"\nclient_id = "visible"\n'


def test_install_skill(home, tmp_path, capsys):
    dest = tmp_path / "skills" / "paper-queue"
    assert cli.main(["install-skill", "--dest", str(dest)]) == 0
    written = dest / "SKILL.md"
    assert written.read_text() == SKILL_MD
    assert str(written) in capsys.readouterr().out
    # default goes under (patched) ~/.claude
    assert cli.main(["install-skill"]) == 0
    assert (cli.Path.home() / ".claude/skills/paper-queue/SKILL.md").exists()


def test_skill_frontmatter():
    assert SKILL_MD.startswith("---\nname: paper-queue\n")
    front = SKILL_MD.split("---")[1]
    assert "description:" in front and "save for later" in front
    for tool in ("add_paper", "upcoming", "mark_read", "remove_paper"):
        assert tool in SKILL_MD


def test_doctor_reports_fixes(home, monkeypatch, capsys):
    import paperqueue.calendar as calmod

    class FakeCal:
        name = "apple"

        def check(self):
            return False, "calendar 'Reading List' not found"

    monkeypatch.setenv("PAPERQUEUE_CALENDAR", "apple")
    monkeypatch.setattr(calmod, "get_backend", lambda cfg: FakeCal())
    assert cli.main(["doctor"]) == 1
    out = capsys.readouterr().out
    assert "[ok] Python" in out and "[ok] Config" in out and "[ok] Database" in out
    assert "Automation" in out
    assert "[FAIL] Calendar (apple)" in out and "fix: create an iCloud calendar" in out
    assert "claude mcp add -s user paperqueue -- paperqueue mcp" in out


def test_mendeley_login_uses_webbrowser(home, monkeypatch, capsys):
    import webbrowser

    from paperqueue.libraries import mendeley

    seen = {}

    def fake_login(cfg, open_url=None, **kw):
        seen["open"] = open_url
        return cfg.home / "mendeley_token.json"

    monkeypatch.setattr(mendeley, "login", fake_login)
    assert cli.main(["mendeley", "login"]) == 0
    assert seen["open"] is webbrowser.open
    assert "Logged in" in capsys.readouterr().out


def test_serve_lan_prints_qr_and_runs_maintenance(home, monkeypatch, capsys):
    import uvicorn

    import paperqueue.server as server

    maintained = threading.Event()

    class FakeQueue:
        def maintain(self):
            maintained.set()

    seen = {}
    monkeypatch.setattr(cli, "make_queue", lambda cfg=None: FakeQueue())
    monkeypatch.setattr(server, "create_app", lambda q, lan=False: seen.setdefault("lan", lan) or "app")

    def fake_run(app, host, port, **kw):
        seen.update(host=host, port=port)
        assert maintained.wait(5)

    monkeypatch.setattr(uvicorn, "run", fake_run)
    assert cli.main(["serve", "--lan", "--port", "9123"]) == 0
    out = capsys.readouterr().out
    assert seen == {"lan": True, "host": "0.0.0.0", "port": 9123}
    assert ".local:9123/?token=tok-SECRET-1" in out
    assert "http://localhost:9123/" in out
    assert "█" in out or "▀" in out or "▄" in out  # terminal QR


def test_serve_no_maintenance(home, monkeypatch, capsys):
    import uvicorn

    import paperqueue.server as server

    class FakeQueue:
        def maintain(self):
            raise AssertionError("maintenance should not run")

    monkeypatch.setattr(cli, "make_queue", lambda cfg=None: FakeQueue())
    monkeypatch.setattr(server, "create_app", lambda q, lan=False: "app")
    seen = {}
    monkeypatch.setattr(uvicorn, "run", lambda app, host, port, **kw: seen.update(host=host))
    assert cli.main(["serve", "--no-maintenance"]) == 0
    assert seen["host"] == "127.0.0.1"
    assert "?token=" not in capsys.readouterr().out
