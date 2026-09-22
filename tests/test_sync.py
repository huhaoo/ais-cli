"""Tests for Seafile sync (HTTP layer is faked; no network)."""

import io
import json
import zipfile

import pytest

from ais import sync
from ais.core import AisError
from ais.sync import SeafileClient

PW = "correct-horse-battery"


class FakeResponse:
    def __init__(self, status=200, text="", content=b"", url="", ctype=None):
        self.status_code = status
        self.text = text if content == b"" else content.decode("utf-8", "replace")
        self.content = content if content != b"" else text.encode()
        self.url = url
        self.headers = {"Content-Type": ctype} if ctype else {}

    @property
    def ok(self):
        return self.status_code < 400

    def json(self):
        return json.loads(self.text)


class FakeSession:
    """Routes (method, url-substring) -> FakeResponse; records all calls."""

    def __init__(self, routes=None):
        self.routes = routes or {}
        self.calls = []

    def request(self, method, url, **kw):
        self.calls.append((method, url, kw))
        # longest url fragment wins, so /api2/repos/rid1/upload-link/
        # is not shadowed by /api2/repos/
        for (m, frag), resp in sorted(self.routes.items(),
                                      key=lambda kv: -len(kv[0][1])):
            if m == method and frag in url:
                return resp
        return FakeResponse(status=404, text="not found")


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("AIS_HOME", str(tmp_path))
    monkeypatch.setenv("AIS_SYNC_PASSWORD", PW)
    (tmp_path / ".codex").mkdir()
    return tmp_path


def make_routes(routes):
    return FakeSession(routes)


def client_with(routes):
    return SeafileClient("https://sf.example.com", "tok-123",
                         session=make_routes(routes))


# ------------------------------------------------------------------ crypto

def test_encrypt_decrypt_roundtrip():
    blob = sync.encrypt_bytes(b"hello", PW)
    assert sync.decrypt_bytes(blob, PW) == b"hello"


def test_wrong_password_rejected():
    blob = sync.encrypt_bytes(b"hello", PW)
    with pytest.raises(AisError, match="wrong password"):
        sync.decrypt_bytes(blob, "nope")


def test_token_roundtrip():
    enc = sync.encrypt_token("tok-123", PW)
    assert "tok-123" not in enc
    assert sync.decrypt_token(enc, PW) == "tok-123"


# ------------------------------------------------------------------ setup

def test_setup_stores_encrypted_token(home, monkeypatch):
    routes = FakeSession({
        ("GET", "/api2/auth/ping/"): FakeResponse(text='{"pong": true}'),
        ("GET", "/api2/repos/"): FakeResponse(text="[]"),
        ("POST", "/api2/repos/"): FakeResponse(text='{"repo_id": "rid1", "repo_name": "ais"}'),
    })
    monkeypatch.setattr(sync, "SeafileClient",
                        lambda url, token, session=None: SeafileClient(url, token, session=routes))
    cfg = sync.setup(home, "https://sf.example.com/", "tok-123", "ais", PW)
    assert cfg["repo_id"] == "rid1"
    raw = (home / ".config" / "ais" / "sync.json").read_text()
    assert "tok-123" not in raw  # plaintext token never stored
    assert sync.decrypt_token(json.loads(raw)["token_enc"], PW) == "tok-123"


def test_setup_finds_existing_repo(home, monkeypatch):
    routes = FakeSession({
        ("GET", "/api2/auth/ping/"): FakeResponse(text='{"pong": true}'),
        ("GET", "/api2/repos/"): FakeResponse(
            text='[{"id": "existing-rid", "name": "ais"}]'),
    })
    monkeypatch.setattr(sync, "SeafileClient",
                        lambda url, token, session=None: SeafileClient(url, token, session=routes))
    cfg = sync.setup(home, "https://sf.example.com", "tok-123", "ais", PW)
    assert cfg["repo_id"] == "existing-rid"
    assert not any(m == "POST" for m, _, _ in routes.calls)  # no create


def test_setup_bad_token_fails_fast(home, monkeypatch):
    routes = FakeSession({("GET", "/api2/auth/ping/"): FakeResponse(status=401)})
    monkeypatch.setattr(sync, "SeafileClient",
                        lambda url, token, session=None: SeafileClient(url, token, session=routes))
    with pytest.raises(AisError, match="unauthorized"):
        sync.setup(home, "https://sf.example.com", "bad", "ais", PW)
    assert not sync.sync_config_path(home).exists()  # nothing stored


# ------------------------------------------------------------------ push

def configure(home, monkeypatch, routes):
    monkeypatch.setattr(sync, "SeafileClient",
                        lambda url, token, session=None: SeafileClient(url, token, session=routes))
    (home / ".config" / "ais" / "codex" / "p1").mkdir(parents=True)
    (home / ".config" / "ais" / "codex" / "p1" / "config.toml").write_text('model = "m"\n')
    (home / ".config" / "ais" / "claude" / "q1").mkdir(parents=True)
    (home / ".config" / "ais" / "claude" / "q1" / "settings.json").write_text('{"env": {}}\n')
    sync.save_sync_config(home, {"v": 1, "url": "https://sf.example.com",
                                 "repo_name": "ais", "repo_id": "rid1",
                                 "remote_path": "/ais-profiles.zip",
                                 "token_enc": sync.encrypt_token("tok-123", PW)})


def test_push_uploads_encrypted_archive(home, monkeypatch):
    routes = FakeSession({
        ("GET", "/api2/auth/ping/"): FakeResponse(text='{"pong": true}'),
        ("GET", "/upload-link/"): FakeResponse(text='"https://sf.example.com/seafhttp/upload-api/rid1"'),
        ("POST", "/seafhttp/upload-api/"): FakeResponse(text='[{"name": "ais-profiles.zip"}]'),
    })
    configure(home, monkeypatch, routes)
    msg = sync.push(home, PW)
    assert "pushed 2 file(s)" in msg

    uploads = [kw for m, u, kw in routes.calls
               if m == "POST" and "upload-api" in u]
    assert uploads and uploads[0]["data"]["parent_dir"] == "/"
    assert uploads[0]["data"]["replace"] == 1
    fname, blob = uploads[0]["files"]["file"]
    assert fname == "ais-profiles.zip"

    files, skipped = sync.extract_archive(blob, PW)
    assert skipped == []
    assert files["codex/p1/config.toml"] == b'model = "m"\n'
    # archive is genuinely AES-encrypted: not a plain zip
    with pytest.raises(Exception):
        zipfile.ZipFile(io.BytesIO(blob)).read("codex/p1/config.toml")


def test_push_refuses_empty_without_force(home, monkeypatch):
    routes = FakeSession({
        ("GET", "/api2/auth/ping/"): FakeResponse(text='{"pong": true}'),
    })
    configure(home, monkeypatch, routes)
    import shutil
    shutil.rmtree(home / ".config" / "ais" / "codex")
    shutil.rmtree(home / ".config" / "ais" / "claude")
    with pytest.raises(AisError, match="no local profiles"):
        sync.push(home, PW)


# ------------------------------------------------------------------ pull

def pull_routes_with(blob):
    return FakeSession({
        ("GET", "/file/"): FakeResponse(content=blob),
    })


def test_pull_replaces_local_profiles(home, monkeypatch):
    configure(home, monkeypatch, FakeSession())  # just seed local profiles
    archive = sync.build_archive({
        "codex/newprof/config.toml": 'model = "from-remote"\n',
        "claude/newprof/settings.json": '{"env": {"ANTHROPIC_BASE_URL": "x"}}\n',
    }, PW)
    routes = pull_routes_with(archive)
    monkeypatch.setattr(sync, "SeafileClient",
                        lambda url, token, session=None: SeafileClient(url, token, session=routes))
    msg = sync.pull(home, PW, yes=True)
    assert "pulled 2 file(s)" in msg
    base = home / ".config" / "ais"
    assert (base / "codex" / "newprof" / "config.toml").read_text() == 'model = "from-remote"\n'
    assert not (base / "codex" / "p1").exists()  # replaced, not merged
    assert (base / "claude" / "newprof" / "settings.json").is_file()


def test_pull_confirm_declined_leaves_local_untouched(home, monkeypatch):
    configure(home, monkeypatch, FakeSession())
    archive = sync.build_archive({"codex/x/config.toml": 'model = "x"\n'}, PW)
    monkeypatch.setattr(sync, "SeafileClient",
                        lambda url, token, session=None: SeafileClient(url, token, session=pull_routes_with(archive)))
    with pytest.raises(AisError, match="aborted"):
        sync.pull(home, PW, yes=False, confirm=lambda files: False)
    assert (home / ".config" / "ais" / "codex" / "p1" / "config.toml").is_file()


def test_pull_wrong_password_untouches_local(home, monkeypatch):
    configure(home, monkeypatch, FakeSession())
    archive = sync.build_archive({"codex/x/config.toml": 'model = "x"\n'}, PW)
    monkeypatch.setattr(sync, "SeafileClient",
                        lambda url, token, session=None: SeafileClient(url, token, session=pull_routes_with(archive)))
    with pytest.raises(AisError, match="wrong password"):
        sync.pull(home, PW + "typo", yes=True)
    assert (home / ".config" / "ais" / "codex" / "p1" / "config.toml").is_file()


def test_pull_rejects_zip_slip_and_invalid_files(home, monkeypatch):
    configure(home, monkeypatch, FakeSession())
    # hand-built archive with path traversal and an invalid config.toml
    import pyzipper
    buf = io.BytesIO()
    with pyzipper.AESZipFile(buf, "w", encryption=pyzipper.WZ_AES) as z:
        z.setpassword(PW.encode())
        z.writestr("../evil.txt", b"boom")
        z.writestr("codex/bad/config.toml", b"not [ valid toml")
    monkeypatch.setattr(sync, "SeafileClient",
                        lambda url, token, session=None: SeafileClient(url, token, session=pull_routes_with(buf.getvalue())))
    with pytest.raises(AisError, match="invalid files"):
        sync.pull(home, PW, yes=True)
    assert not (home / ".config" / "ais" / "codex" / "bad").exists()
    assert not (home / "evil.txt").exists()
    assert (home / ".config" / "ais" / "codex" / "p1" / "config.toml").is_file()

    # archive that is valid except for a traversal member -> skipped, not applied
    buf2 = io.BytesIO()
    with pyzipper.AESZipFile(buf2, "w", encryption=pyzipper.WZ_AES) as z:
        z.setpassword(PW.encode())
        z.writestr("../evil.txt", b"boom")
        z.writestr("codex/ok/config.toml", 'model = "ok"\n')
    monkeypatch.setattr(sync, "SeafileClient",
                        lambda url, token, session=None: SeafileClient(url, token, session=pull_routes_with(buf2.getvalue())))
    msg = sync.pull(home, PW, yes=True)
    assert "evil.txt" in msg  # reported as skipped
    assert not (home / "evil.txt").exists()
    assert (home / ".config" / "ais" / "codex" / "ok" / "config.toml").is_file()


def test_pull_404_reports_not_found(home, monkeypatch):
    configure(home, monkeypatch, FakeSession())
    monkeypatch.setattr(sync, "SeafileClient",
                        lambda url, token, session=None: SeafileClient(url, token, session=FakeSession()))
    with pytest.raises(AisError, match="push first"):
        sync.pull(home, PW, yes=True)


def test_pull_follows_json_quoted_download_link(home, monkeypatch):
    """Seafile Pro 11 answers 200 with a JSON-quoted file URL, not a 302."""
    configure(home, monkeypatch, FakeSession())
    archive = sync.build_archive({"codex/j/config.toml": 'model = "j"\n'}, PW)
    routes = FakeSession({
        ("GET", "/file/"): FakeResponse(
            text='"https://sf.example.com/seafhttp/files/tok1/ais-profiles.zip"'),
        ("GET", "/seafhttp/files/"): FakeResponse(content=archive),
    })
    monkeypatch.setattr(sync, "SeafileClient",
                        lambda url, token, session=None: SeafileClient(url, token, session=routes))
    msg = sync.pull(home, PW, yes=True)
    assert "pulled 1 file(s)" in msg
    assert (home / ".config" / "ais" / "codex" / "j" / "config.toml").is_file()
    # the archive came from the absolute link found in the JSON body
    assert any(u.startswith("https://sf.example.com/seafhttp/files/")
               for m, u, kw in routes.calls)


def test_pull_non_zip_download_shows_what_arrived(home, monkeypatch):
    configure(home, monkeypatch, FakeSession())
    routes = FakeSession({("GET", "/file/"): FakeResponse(
        content=b"<html>please sign in</html>",
        url="https://sf.example.com/accounts/login/", ctype="text/html")})
    monkeypatch.setattr(sync, "SeafileClient",
                        lambda url, token, session=None: SeafileClient(url, token, session=routes))
    with pytest.raises(AisError, match=r"text/html.*please sign in"):
        sync.pull(home, PW, yes=True)
    assert (home / ".config" / "ais" / "codex" / "p1" / "config.toml").is_file()


def test_pull_empty_download_rejected(home, monkeypatch):
    configure(home, monkeypatch, FakeSession())
    routes = FakeSession({("GET", "/file/"): FakeResponse(content=b"")})
    monkeypatch.setattr(sync, "SeafileClient",
                        lambda url, token, session=None: SeafileClient(url, token, session=routes))
    with pytest.raises(AisError, match="0 byte"):
        sync.pull(home, PW, yes=True)


def test_pull_truncated_archive_is_reported(home, monkeypatch):
    configure(home, monkeypatch, FakeSession())
    blob = sync.build_archive({"codex/x/config.toml": 'model = "x"\n'}, PW)
    monkeypatch.setattr(sync, "SeafileClient",
                        lambda url, token, session=None: SeafileClient(url, token, session=pull_routes_with(blob[:len(blob) // 2])))
    with pytest.raises(AisError, match="truncated"):
        sync.pull(home, PW, yes=True)
    assert (home / ".config" / "ais" / "codex" / "p1" / "config.toml").is_file()


# ------------------------------------------------------------------ passwd

def test_change_password_reencrypts_token(home, monkeypatch):
    configure(home, monkeypatch, FakeSession())
    sync.change_password(home, PW, "new-pw")
    cfg = json.loads(sync.sync_config_path(home).read_text())
    assert sync.decrypt_token(cfg["token_enc"], "new-pw") == "tok-123"
    with pytest.raises(AisError):
        sync.decrypt_token(cfg["token_enc"], PW)


# ------------------------------------------------------------------ real HTTP integration
# A tiny in-process Seafile-compatible server: exercises the actual requests
# stack (multipart upload, quoted upload-link, 302 download redirect).

import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse


class SeafileMockHandler(BaseHTTPRequestHandler):
    token = "tok-123"
    base = "http://127.0.0.1"
    store = {}  # {"upload": bytes}

    def log_message(self, *args):
        pass

    def _send(self, status, body: bytes, ctype="application/json",
              location=None):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        if location:
            self.send_header("Location", location)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, status=200):
        self._send(status, json.dumps(obj).encode())

    def _authed(self):
        return self.headers.get("Authorization") == f"Token {self.token}"

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/api2/auth/ping/":
            return self._json({"pong": True} if self._authed()
                              else {"error": "bad token"},
                              200 if self._authed() else 401)
        if path == "/api2/repos/":
            return self._json([{"id": "rid1", "name": "ais"}]
                              if self._authed() else [], 200 if self._authed() else 403)
        if path == "/api2/repos/rid1/upload-link/":
            # real Seafile returns the URL wrapped in quotes
            return self._send(200, f'"{self.base}/seafhttp/upload-api/rid1"'.encode(),
                              ctype="text/plain")
        if path == "/api2/repos/rid1/file/":
            return self._send(302, b"", location=f"{self.base}/files/f1")
        if path == "/files/f1":
            return self._send(200, self.store.get("upload", b""),
                              ctype="application/zip")
        if path == "/api2/repos/rid1/file/detail/":
            data = self.store.get("upload", b"")
            return self._json({"name": "ais-profiles.zip", "size": len(data),
                               "mtime": 1700000000})
        self._json({"message": "Not Found"}, 404)

    def do_POST(self):
        if not self.path.startswith("/seafhttp/upload-api/"):
            return self._json({"message": "Not Found"}, 404)
        if not self._authed():
            return self._json({}, 403)
        body = self.rfile.read(int(self.headers["Content-Length"]))
        boundary = self.headers["Content-Type"].split("boundary=")[1].encode()
        for seg in body.split(b"--" + boundary):
            if b'name="file"' not in seg:
                continue
            content = seg[seg.find(b"\r\n\r\n") + 4:seg.rfind(b"\r\n")]
            self.store["upload"] = content
        self._json([{"name": "ais-profiles.zip", "id": "f1"}])


@pytest.fixture
def seafile_server():
    handler = type("Handler", (SeafileMockHandler,), {"store": {}})
    srv = HTTPServer(("127.0.0.1", 0), handler)
    handler.base = f"http://127.0.0.1:{srv.server_port}"
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield handler.base, handler.store
    srv.shutdown()


def test_real_http_roundtrip(home, seafile_server):
    base, store = seafile_server

    # bad token is rejected server-side before anything is stored
    with pytest.raises(AisError, match="unauthorized"):
        sync.setup(home, base, "wrong-token", "ais", PW)

    cfg = sync.setup(home, base, "tok-123", "ais", PW)
    assert cfg["repo_id"] == "rid1"

    pd = home / ".config" / "ais" / "codex" / "p1"
    pd.mkdir(parents=True)
    (pd / "config.toml").write_text('model = "m"\n')

    msg = sync.push(home, PW)
    assert "pushed 1 file(s)" in msg
    assert store["upload"]

    # simulate a second machine: wipe local profiles, pull them back
    import shutil
    shutil.rmtree(home / ".config" / "ais" / "codex")
    sync.pull(home, PW, yes=True)
    assert (pd / "config.toml").read_text() == 'model = "m"\n'

    detail = sync.remote_status(home, PW)
    assert detail["size"] == len(store["upload"])


# ------------------------------------------------------------------ CLI layer

def test_cli_setup_prompts_and_pull_flow(home, monkeypatch):
    from typer.testing import CliRunner
    from ais.cli import app as cli_app
    runner = CliRunner()
    monkeypatch.delenv("AIS_SYNC_PASSWORD", raising=False)

    routes = FakeSession({
        ("GET", "/api2/auth/ping/"): FakeResponse(text='{"pong": true}'),
        ("GET", "/api2/repos/"): FakeResponse(text="[]"),
        ("POST", "/api2/repos/"): FakeResponse(text='{"repo_id": "rid1"}'),
    })
    monkeypatch.setattr(sync, "SeafileClient",
                        lambda url, token, session=None: SeafileClient(url, token, session=routes))

    # interactive setup: url, token(hidden), repo(default), password twice
    r = runner.invoke(cli_app, ["sync", "setup"],
                      input="https://sf.example.com\ntok-123\n\npw-1\npw-1\n")
    assert r.exit_code == 0, r.output
    assert "sync configured" in r.output

    # mismatched password confirmation fails
    monkeypatch.delenv("AIS_SYNC_PASSWORD", raising=False)
    r = runner.invoke(cli_app, ["sync", "setup"],
                      input="https://sf.example.com\ntok-123\n\npw-1\npw-2\n")
    assert r.exit_code != 0

    # push/pull via CLI with AIS_SYNC_PASSWORD set (no prompts)
    monkeypatch.setenv("AIS_SYNC_PASSWORD", "pw-1")
    pd = home / ".config" / "ais" / "codex" / "cli1"
    pd.mkdir(parents=True)
    (pd / "config.toml").write_text('model = "c"\n')
    routes.routes[("GET", "/upload-link/")] = FakeResponse(
        text='"https://sf.example.com/seafhttp/upload-api/rid1"')
    routes.routes[("POST", "/seafhttp/upload-api/")] = FakeResponse(
        text='[{"name": "ais-profiles.zip"}]')
    r = runner.invoke(cli_app, ["sync", "push"])
    assert r.exit_code == 0, r.output
    assert "pushed 1 file(s)" in r.output

    # status (local only, no password needed) then unconfigured machine error
    r = runner.invoke(cli_app, ["sync", "status"])
    assert r.exit_code == 0 and "sf.example.com" in r.output
    monkeypatch.setenv("AIS_HOME", str(home / "elsewhere"))
    r = runner.invoke(cli_app, ["sync", "status"])
    assert r.exit_code == 1 and "not configured" in r.output
