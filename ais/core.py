"""Core primitives: paths, atomic writes, backups, state, baseline, profiles.

Everything ais manages lives under ``<home>/.config/ais``. ``<home>`` defaults
to ``Path.home()`` and can be overridden with the ``AIS_HOME`` environment
variable (mainly useful for tests).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

BACKUP_KEEP = 50
NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")


class AisError(Exception):
    """Fatal, user-facing error."""


@dataclass(frozen=True)
class App:
    """Description of one managed application (codex or claude)."""

    name: str                                  # subcommand name
    live_dir_name: str                         # dotdir under home, e.g. ".codex"
    live_file: str                             # live config filename, e.g. "config.toml"
    binary: str                                # executable launched by `run`
    validate: Callable[[str], None]            # raises on invalid content
    auth_files: tuple[str, ...]                # official-login files (never touched)
    prepare_use: Callable[[Path, str], str]    # (profile_dir, content) -> content
    post_save: Callable[..., list]             # (app, home, profile_dir) -> warnings


def get_home() -> Path:
    override = os.environ.get("AIS_HOME")
    return Path(override).expanduser() if override else Path.home()


# ---------------------------------------------------------------- paths

def ais_dir(home: Path) -> Path:
    return home / ".config" / "ais"


def app_profiles_dir(home: Path, app: App) -> Path:
    return ais_dir(home) / app.name


def profile_dir(home: Path, app: App, name: str) -> Path:
    return app_profiles_dir(home, app) / name


def live_dir(home: Path, app: App) -> Path:
    return home / app.live_dir_name


def live_path(home: Path, app: App) -> Path:
    return live_dir(home, app) / app.live_file


def baseline_dir(home: Path, app: App) -> Path:
    return ais_dir(home) / "baseline" / app.name


def backups_dir(home: Path, app: App) -> Path:
    return ais_dir(home) / "backups" / app.name


def state_path(home: Path) -> Path:
    return ais_dir(home) / "state.json"


# ---------------------------------------------------------------- names

def check_name(name: str) -> None:
    if not NAME_RE.fullmatch(name) or name in {".", ".."}:
        raise AisError(
            f"invalid profile name {name!r}: use letters, digits, '.', '_' or '-'"
        )


# ---------------------------------------------------------------- hashing

def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# ---------------------------------------------------------------- writing

def atomic_write_bytes(path: Path, data: bytes) -> None:
    """Write bytes to path via a temp file + fsync + atomic rename."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with open(tmp, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def atomic_write(path: Path, data: str) -> None:
    atomic_write_bytes(path, data.encode("utf-8"))


def backup_live(home: Path, app: App) -> Optional[Path]:
    """Copy the current live config into backups/. Returns None if absent."""
    live = live_path(home, app)
    if not live.is_file():
        return None
    bdir = backups_dir(home, app)
    bdir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    target = bdir / f"{stamp}-{app.live_file}"
    n = 0
    while target.exists():
        n += 1
        target = bdir / f"{stamp}-{n}-{app.live_file}"
    shutil.copy2(live, target)
    prune_backups(bdir, BACKUP_KEEP)
    return target


def prune_backups(bdir: Path, keep: int) -> None:
    files = sorted(p for p in bdir.iterdir() if p.is_file())
    for old in files[:-keep] if keep > 0 else files:
        old.unlink(missing_ok=True)


# ---------------------------------------------------------------- state.json

def read_state(home: Path, strict: bool = True) -> dict:
    p = state_path(home)
    if not p.is_file():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("expected a JSON object")
    except (OSError, ValueError) as e:
        if strict:
            raise AisError(f"cannot read {p}: {e}")
        return {}
    return data


def write_state(home: Path, state: dict) -> None:
    atomic_write(state_path(home), json.dumps(state, indent=2, ensure_ascii=False) + "\n")


def set_state(home: Path, app: App, *, current: Optional[str], live_hash: Optional[str],
              official: bool = False) -> None:
    state = read_state(home)
    state[app.name] = {"current": current, "hash": live_hash, "official": official}
    write_state(home, state)


def state_entry(home: Path, app: App) -> dict:
    entry = read_state(home, strict=False).get(app.name)
    return entry if isinstance(entry, dict) else {}


def resolve_current(home: Path, app: App) -> str:
    """Human-readable answer to "which profile is active right now?"."""
    entry = state_entry(home, app)
    live = live_path(home, app)
    cur = entry.get("current")
    if cur:
        if not live.is_file():
            return f"{cur} (missing)"
        return cur if entry.get("hash") == sha256_file(live) else f"{cur} (modified)"
    if entry.get("official"):
        h = entry.get("hash")
        if not live.is_file():
            return "official"
        if h and h == sha256_file(live):
            return "official"
    return "unmanaged"


# ---------------------------------------------------------------- baseline

def baseline_captured(home: Path, app: App) -> bool:
    return (baseline_dir(home, app) / "existed.json").is_file()


def ensure_baseline(home: Path, app: App) -> bool:
    """Capture the pre-ais live config once, never overwriting. True if new."""
    if baseline_captured(home, app):
        return False
    bdir = baseline_dir(home, app)
    bdir.mkdir(parents=True, exist_ok=True)
    live = live_path(home, app)
    existed = live.is_file()
    if existed:
        shutil.copy2(live, bdir / app.live_file)
    atomic_write(bdir / "existed.json",
                 json.dumps({app.live_file: existed}, indent=2) + "\n")
    return True


def read_baseline(home: Path, app: App) -> "tuple[bool, Optional[str]]":
    """Return (existed, content); content is None when the file did not exist."""
    meta = json.loads((baseline_dir(home, app) / "existed.json").read_text(encoding="utf-8"))
    if not meta.get(app.live_file):
        return False, None
    return True, (baseline_dir(home, app) / app.live_file).read_text(encoding="utf-8")


def reset_baseline(home: Path, app: App) -> None:
    bdir = baseline_dir(home, app)
    if bdir.exists():
        shutil.rmtree(bdir)
    ensure_baseline(home, app)


# ---------------------------------------------------------------- profiles

def list_profiles(home: Path, app: App) -> "list[str]":
    pdir = app_profiles_dir(home, app)
    if not pdir.is_dir():
        return []
    return sorted(d.name for d in pdir.iterdir()
                  if d.is_dir() and (d / app.live_file).is_file())


def profile_exists(home: Path, app: App, name: str) -> bool:
    return (profile_dir(home, app, name) / app.live_file).is_file()


# ---------------------------------------------------------------- auth

def auth_status(home: Path, app: App) -> str:
    """Report official-login presence without reading or parsing contents."""
    try:
        for rel in app.auth_files:
            p = live_dir(home, app) / rel
            if p.is_file() and p.stat().st_size > 0:
                return "detected"
        return "not detected"
    except OSError:
        return "unknown"
