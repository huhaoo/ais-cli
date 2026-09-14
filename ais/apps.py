"""Per-application adapters.

Codex-specific behaviour lives here: ``model_catalog_json`` rewriting so a
profile's ``models.json`` travels with it. Everything else is verbatim file
copying — ais never reinterprets, rebuilds or simplifies user config.
"""

from __future__ import annotations

import json
import os
import re
import shutil
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10
    import tomli as tomllib

from .core import App

CATALOG_KEY = "model_catalog_json"

_TOML_STRING_VALUE = re.compile(
    r"(?m)^(\s*model_catalog_json\s*=\s*)(\"[^\"\n]*\"|'[^'\n]*')"
)
_TABLE_HEADER = re.compile(r"(?m)^\s*\[")


def _validate_toml(text: str) -> None:
    tomllib.loads(text)


def _validate_json(text: str) -> None:
    json.loads(text)


def _identity_prepare(profile_dir: Path, content: str) -> str:
    return content


def _no_post_save(app: App, home: Path, profile_dir: Path) -> list:
    return []


def _toml_quote(value: str) -> str:
    # JSON string escaping is valid TOML basic-string escaping.
    return json.dumps(value)


def rewrite_catalog(text: str, new_value: str) -> str:
    """Point ``model_catalog_json`` at new_value, keeping the file verbatim.

    Only a top-level occurrence is rewritten (matches after the first
    ``[table]`` header are ignored); if the key is absent it is inserted
    before the first table header. Comments and unknown fields survive.
    """
    line = f"{CATALOG_KEY} = {_toml_quote(new_value)}"
    header = _TABLE_HEADER.search(text)
    top_end = header.start() if header else len(text)
    top, rest = text[:top_end], text[top_end:]

    m = _TOML_STRING_VALUE.search(top)
    if m:
        return top[:m.start()] + m.group(1) + _toml_quote(new_value) + top[m.end():] + rest
    if header:
        prefix = top.rstrip("\n") + "\n\n" if top.strip() else ""
        return prefix + line + "\n" + rest
    return (text.rstrip("\n") + "\n" if text.strip() else "") + line + "\n"


def codex_prepare_use(profile_dir: Path, content: str) -> str:
    """At switch time, make model_catalog_json point at the profile's models.json."""
    models = profile_dir / "models.json"
    if models.is_file():
        content = rewrite_catalog(content, str(models.resolve()))
    return content


def codex_post_save(app: App, home: Path, profile_dir: Path) -> list:
    """After the live config.toml was copied into the profile, handle models.json.

    If it references a local catalog file, copy that file into the profile as
    ``models.json`` and rewrite the profile copy to the portable value
    ``"models.json"``. Returns a list of user-facing warnings.
    """
    config = profile_dir / "config.toml"
    text = config.read_text(encoding="utf-8")
    warnings: list = []
    try:
        parsed = tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        return [f"profile config.toml is invalid TOML ({e}); models.json not handled"]

    ref = parsed.get(CATALOG_KEY)
    if not isinstance(ref, str) or not ref:
        return warnings

    cand = Path(os.path.expanduser(ref))
    candidates = ([cand] if cand.is_absolute() else
                  [home / app.live_dir_name / cand, Path.cwd() / cand, profile_dir / cand])
    src = next((c for c in candidates if c.is_file()), None)
    dst = profile_dir / "models.json"
    if src is None:
        return [f"model_catalog_json points to {ref!r}, which does not exist; "
                f"kept the original path"]
    if src.resolve() != dst.resolve():
        shutil.copy2(src, dst)
    config.write_text(rewrite_catalog(text, "models.json"), encoding="utf-8")
    return warnings


CODEX = App(
    name="codex",
    live_dir_name=".codex",
    live_file="config.toml",
    binary="codex",
    validate=_validate_toml,
    auth_files=("auth.json",),
    prepare_use=codex_prepare_use,
    post_save=codex_post_save,
)

CLAUDE = App(
    name="claude",
    live_dir_name=".claude",
    live_file="settings.json",
    binary="claude",
    validate=_validate_json,
    auth_files=(".credentials.json",),
    prepare_use=_identity_prepare,
    post_save=_no_post_save,
)

APPS = {"codex": CODEX, "claude": CLAUDE}
APPS_ORDER = (CODEX, CLAUDE)
