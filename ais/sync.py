"""Seafile sync: password-encrypted zip of provider profiles, pushed/pulled
via the Seafile Web API.

Design:
- ``~/.config/ais/sync.json`` stores url / repo / remote path plus the API
  token encrypted with the sync password (symmetric: same password encrypts
  and decrypts). sync.json itself is never synced.
- Only the portable part of ais travels: the ``codex/`` and ``claude/``
  profile directories. state.json, baseline/ and backups/ are machine-local.
- The archive is a standard AES zip (WZ_AES): recoverable with 7-Zip/unzip
  using the same password, independent of ais.
"""

from __future__ import annotations

import base64
import io
import json
import shutil
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import urlencode, urljoin

import pyzipper
import requests

from . import core
from .apps import APPS_ORDER, validate_json, validate_toml
from .core import AisError

SYNC_CONFIG_NAME = "sync.json"
REPO_NAME_DEFAULT = "ais"
REMOTE_PATH_DEFAULT = "/ais-profiles.zip"
TIMEOUT = 30
MAX_DOWNLOAD = 100 * 1024 * 1024  # 100 MB sanity limit
PROFILE_ROOTS = ("codex", "claude")


# ---------------------------------------------------------------- crypto

def encrypt_bytes(data: bytes, password: str) -> bytes:
    """AES-encrypted zip holding one entry; decrypt with the same password."""
    buf = io.BytesIO()
    with pyzipper.AESZipFile(buf, "w", compression=pyzipper.ZIP_DEFLATED,
                             encryption=pyzipper.WZ_AES) as z:
        z.setpassword(password.encode("utf-8"))
        z.writestr("data", data)
    return buf.getvalue()


def decrypt_bytes(blob: bytes, password: str) -> bytes:
    try:
        with pyzipper.AESZipFile(io.BytesIO(blob)) as z:
            z.setpassword(password.encode("utf-8"))
            return z.read("data")
    except RuntimeError:
        raise AisError("wrong password (decryption failed)")


def encrypt_token(token: str, password: str) -> str:
    return base64.b64encode(encrypt_bytes(token.encode("utf-8"), password)).decode("ascii")


def decrypt_token(token_enc: str, password: str) -> str:
    try:
        blob = base64.b64decode(token_enc.encode("ascii"))
    except (ValueError, TypeError):
        raise AisError("stored token is corrupt; run 'ais sync setup' again")
    return decrypt_bytes(blob, password).decode("utf-8")


# ---------------------------------------------------------------- sync config

def sync_config_path(home: Path) -> Path:
    return core.ais_dir(home) / SYNC_CONFIG_NAME


def load_sync_config(home: Path) -> dict:
    p = sync_config_path(home)
    if not p.is_file():
        raise AisError("sync is not configured yet; run 'ais sync setup'")
    try:
        cfg = json.loads(p.read_text(encoding="utf-8"))
        for key in ("url", "repo_id", "token_enc"):
            if not cfg.get(key):
                raise ValueError(f"missing field {key!r}")
    except (OSError, ValueError) as e:
        raise AisError(f"cannot read {p}: {e}; run 'ais sync setup'")
    return cfg


def save_sync_config(home: Path, cfg: dict) -> None:
    core.atomic_write(sync_config_path(home),
                      json.dumps(cfg, indent=2, ensure_ascii=False) + "\n")


# ---------------------------------------------------------------- Seafile client

class SeafileClient:
    """Minimal Seafile Web API (api2) client; inject a session for testing."""

    def __init__(self, base_url: str, token: str,
                 session: Optional[requests.Session] = None):
        self.base = base_url.rstrip("/")
        self.s = session if session is not None else requests.Session()
        self.headers = {"Authorization": f"Token {token}"}

    def _request(self, method: str, url: str, **kw) -> requests.Response:
        full = url if url.startswith("http") else self.base + url
        kw.setdefault("timeout", TIMEOUT)
        try:
            return self.s.request(method, full, headers=self.headers, **kw)
        except requests.RequestException as e:
            raise AisError(f"network error talking to Seafile: {e}")

    @staticmethod
    def _check(r: requests.Response, what: str) -> requests.Response:
        if r.status_code in (401, 403):
            raise AisError(f"{what}: unauthorized (HTTP {r.status_code}); "
                           f"check your API token")
        if not r.ok:
            raise AisError(f"{what}: HTTP {r.status_code}: {r.text[:200]}")
        return r

    def ping(self) -> None:
        r = self._request("GET", "/api2/auth/ping/")
        self._check(r, "Seafile ping")

    def find_repo(self, name: str) -> Optional[str]:
        r = self._request("GET", "/api2/repos/")
        self._check(r, "list libraries")
        for repo in r.json():
            if repo.get("name") == name:
                return repo.get("id")
        return None

    def create_repo(self, name: str) -> str:
        r = self._request("POST", "/api2/repos/",
                          data={"name": name, "desc": "ais provider profile sync"})
        self._check(r, "create library")
        data = r.json()
        repo_id = data.get("repo_id") or data.get("id")
        if not repo_id:
            raise AisError(f"create library: unexpected response: {r.text[:200]}")
        return repo_id

    def ensure_repo(self, name: str) -> str:
        return self.find_repo(name) or self.create_repo(name)

    def upload(self, repo_id: str, remote_path: str, data: bytes) -> None:
        r = self._request("GET", f"/api2/repos/{repo_id}/upload-link/")
        self._check(r, "get upload link")
        # Seafile returns the URL quoted as a JSON-ish string
        link = r.text.strip().strip('"').strip("'")
        link = urljoin(self.base + "/", link)
        parent, _, fname = remote_path.rpartition("/")
        r = self._request("POST", link, files={"file": (fname or "ais.zip", data)},
                          data={"parent_dir": parent or "/", "replace": 1})
        self._check(r, "upload")

    def download(self, repo_id: str, remote_path: str) -> bytes:
        q = urlencode({"p": remote_path, "op": "download"})
        r = self._request("GET", f"/api2/repos/{repo_id}/file/?{q}",
                          allow_redirects=True)
        if r.status_code == 404:
            raise AisError(f"remote {remote_path} not found; push first")
        self._check(r, "download")
        if len(r.content) > MAX_DOWNLOAD:
            raise AisError("downloaded archive unexpectedly large; refusing")
        return r.content

    def file_detail(self, repo_id: str, remote_path: str) -> Optional[dict]:
        q = urlencode({"p": remote_path})
        r = self._request("GET", f"/api2/repos/{repo_id}/file/detail/?{q}")
        if r.status_code == 404:
            return None
        self._check(r, "file detail")
        return r.json()


# ---------------------------------------------------------------- archive

def collect_profiles(home: Path) -> "dict[str, bytes]":
    """All profile files under ~/.config/ais/{codex,claude}/ as {relpath: bytes}."""
    base = core.ais_dir(home)
    out: "dict[str, bytes]" = {}
    for a in APPS_ORDER:
        pdir = core.app_profiles_dir(home, a)
        if not pdir.is_dir():
            continue
        for p in sorted(pdir.rglob("*")):
            inside = p.relative_to(pdir)
            if not p.is_file() or any(part.startswith(".") for part in inside.parts):
                continue
            out[p.relative_to(base).as_posix()] = p.read_bytes()
    return out


def build_archive(files: "dict[str, bytes]", password: str) -> bytes:
    buf = io.BytesIO()
    with pyzipper.AESZipFile(buf, "w", compression=pyzipper.ZIP_DEFLATED,
                             encryption=pyzipper.WZ_AES) as z:
        z.setpassword(password.encode("utf-8"))
        for name in sorted(files):
            z.writestr(name, files[name])
    return buf.getvalue()


def _member_ok(name: str) -> bool:
    """True if an archive member may be extracted (zip-slip protection)."""
    if not name or name.startswith(("/","\\")) or ".." in name.split("/"):
        return False
    parts = name.split("/")
    if parts[0] not in PROFILE_ROOTS:
        return False
    return all(parts)


def validate_member(name: str, data: bytes) -> Optional[str]:
    """Return an error string if a file would be invalid on disk, else None."""
    fname = name.rsplit("/", 1)[-1]
    try:
        if fname == "config.toml":
            validate_toml(data.decode("utf-8"))
        elif fname in ("settings.json", "models.json"):
            validate_json(data.decode("utf-8"))
        elif fname == "env.json":
            parsed = json.loads(data.decode("utf-8"))
            if not (isinstance(parsed, dict)
                    and all(isinstance(k, str) and isinstance(v, str)
                            for k, v in parsed.items())):
                return "env.json must map names to strings"
    except AisError:
        raise
    except Exception as e:
        return str(e)
    return None


def extract_archive(blob: bytes, password: str) -> "tuple[dict[str, bytes], list[str]]":
    """Decrypt and read an archive; returns (files, skipped_member_names)."""
    files: "dict[str, bytes]" = {}
    skipped: "list[str]" = []
    try:
        with pyzipper.AESZipFile(io.BytesIO(blob)) as z:
            z.setpassword(password.encode("utf-8"))
            for name in z.namelist():
                if name.endswith("/") or not _member_ok(name):
                    skipped.append(name)
                    continue
                files[name] = z.read(name)
    except RuntimeError:
        raise AisError("wrong password or corrupted archive")
    except Exception as e:
        raise AisError(f"cannot read archive: {e}")
    return files, skipped


def _profile_summary(files: "dict[str, bytes]") -> str:
    names: "dict[str, set]" = {}
    for rel in files:
        parts = rel.split("/")
        if len(parts) >= 2:
            names.setdefault(parts[0], set()).add(parts[1])
    chunks = []
    for app in PROFILE_ROOTS:
        ps = sorted(names.get(app, ()))
        chunks.append(f"{app}: {len(ps)} profile(s)"
                      + (f" ({', '.join(ps)})" if ps else ""))
    return "; ".join(chunks)


def apply_pulled(home: Path, files: "dict[str, bytes]") -> None:
    """Replace the local codex/ and claude/ profile dirs with archive content."""
    base = core.ais_dir(home)
    for root in PROFILE_ROOTS:
        d = base / root
        if d.exists():
            shutil.rmtree(d)
    for rel, data in files.items():
        core.atomic_write_bytes(base / rel, data)


# ---------------------------------------------------------------- flows

def setup(home: Path, url: str, token: str, repo_name: str, password: str) -> dict:
    """Validate connection, find-or-create the library, store encrypted token."""
    url = url.rstrip("/")
    client = SeafileClient(url, token)
    client.ping()
    repo_id = client.ensure_repo(repo_name)
    cfg = {"v": 1, "url": url, "repo_name": repo_name, "repo_id": repo_id,
           "remote_path": REMOTE_PATH_DEFAULT,
           "token_enc": encrypt_token(token, password)}
    save_sync_config(home, cfg)
    return cfg


def change_password(home: Path, old: str, new: str) -> None:
    cfg = load_sync_config(home)
    token = decrypt_token(cfg["token_enc"], old)
    cfg["token_enc"] = encrypt_token(token, new)
    save_sync_config(home, cfg)


def push(home: Path, password: str, force: bool = False) -> str:
    cfg = load_sync_config(home)
    client = SeafileClient(cfg["url"], decrypt_token(cfg["token_enc"], password))
    client.ping()
    files = collect_profiles(home)
    if not files and not force:
        raise AisError("no local profiles to push (syncing an empty set would "
                       "wipe other machines on pull; pass --force)")
    blob = build_archive(files, password)
    client.upload(cfg["repo_id"], cfg.get("remote_path", REMOTE_PATH_DEFAULT), blob)
    return (f"pushed {len(files)} file(s), {len(blob)} bytes to "
            f"{cfg['url']} library '{cfg['repo_name']}'"
            f"{cfg.get('remote_path', REMOTE_PATH_DEFAULT)}")


def pull(home: Path, password: str, *, yes: bool = False,
         confirm: Optional[Callable[["dict[str, bytes]"], bool]] = None) -> str:
    cfg = load_sync_config(home)
    client = SeafileClient(cfg["url"], decrypt_token(cfg["token_enc"], password))
    blob = client.download(cfg["repo_id"], cfg.get("remote_path", REMOTE_PATH_DEFAULT))

    files, skipped = extract_archive(blob, password)
    errors = [f"{rel}: {err}" for rel, data in sorted(files.items())
              if (err := validate_member(rel, data)) is not None]
    if errors:
        raise AisError("archive contains invalid files, nothing applied:\n  "
                       + "\n  ".join(errors))
    if not yes:
        if not confirm or not confirm(files):
            raise AisError("aborted")

    apply_pulled(home, files)
    msg = f"pulled {len(files)} file(s): {_profile_summary(files)}"
    if skipped:
        msg += f"\nskipped non-profile members: {', '.join(sorted(skipped))}"
    return msg


def remote_status(home: Path, password: str) -> Optional[dict]:
    cfg = load_sync_config(home)
    client = SeafileClient(cfg["url"], decrypt_token(cfg["token_enc"], password))
    return client.file_detail(cfg["repo_id"], cfg.get("remote_path", REMOTE_PATH_DEFAULT))
