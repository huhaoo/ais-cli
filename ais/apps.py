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

# AI commit attribution (Co-authored-by trailers Codex appends to commits/PRs)
ATTRIB_KEY = "commit_attribution_enabled"
# [ \t] (not \s) keeps the header match on one line, so a comment on the
# *next* line stays part of the section body instead of being swallowed.
_FEATURES_HEADER = re.compile(r"(?m)^[ \t]*\[features\][ \t]*(?:#.*)?$")
_ATTRIB_VALUE = re.compile(r"(?m)^([ \t]*" + ATTRIB_KEY + r"[ \t]*=[ \t]*)(true|false)")
_ATTRIB_LINE_FALSE = re.compile(
    r"(?m)^[ \t]*" + ATTRIB_KEY + r"[ \t]*=[ \t]*false[ \t]*(?:#.*)?\r?\n?"
)

# Machine-local settings ais never manages: claude's `permissions` object
# (allow/deny/ask rules, defaultMode, additionalDirectories) and codex's
# `[projects]` directory-trust tables. They describe this machine's folders,
# not a provider, so they never travel inside profiles: `save` strips them,
# `use`/`clear` keep whatever the live config already has.
CLAUDE_UNMANAGED_KEYS = ("permissions",)

# Header pattern tolerates quoted key parts containing ']': [projects."/a]b"]
_SECTION_HEADER = re.compile(
    r"(?m)^[ \t]*\[((?:[^\]\"']|\"[^\"]*\"|'[^']*')+)\][ \t]*(#.*)?$"
)


def _codex_sections(text: str) -> "list[tuple[int, int, str]]":
    """Spans (start, end, name) of each top-level TOML table section."""
    matches = list(_SECTION_HEADER.finditer(text))
    return [(m.start(),
             matches[i + 1].start() if i + 1 < len(matches) else len(text),
             m.group(1).strip())
            for i, m in enumerate(matches)]


def _is_projects(name: str) -> bool:
    return name == "projects" or name.startswith("projects.")


def validate_toml(text: str) -> None:
    tomllib.loads(text)


def validate_json(text: str) -> None:
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


def codex_hide_attribution(text: str) -> str:
    """Ensure ``[features] commit_attribution_enabled = false``, file stays verbatim.

    Codex >= 0.155 appends ``Co-authored-by: Codex <noreply@openai.com>`` to
    commit messages and a ``Generated with [Codex](...)`` line to PR bodies when
    the feature is enabled. The key is set inside an existing ``[features]``
    table when there is one (only that table's own section is examined, so a
    same-named key under another table is left alone); otherwise the table is
    appended. Comments and unknown fields survive.
    """
    line = f"{ATTRIB_KEY} = false"
    header = _FEATURES_HEADER.search(text)
    if header is None:
        body = text.rstrip("\n")
        return (body + "\n\n" if body else "") + "[features]\n" + line + "\n"

    rest = text[header.end():]
    nxt = _TABLE_HEADER.search(rest)
    section, tail = (rest[:nxt.start()], rest[nxt.start():]) if nxt else (rest, "")
    if _ATTRIB_VALUE.search(section):
        section = _ATTRIB_VALUE.sub(lambda m: m.group(1) + "false", section, count=1)
    else:
        section = "\n" + line + section
    return text[:header.end()] + section + tail


def codex_strip_attribution(text: str) -> str:
    """Remove the ``commit_attribution_enabled = false`` line ais injects.

    Only the ais-set value (``false``) is removed; a user-authored ``true``
    stays. If the ``[features]`` table becomes empty, its header goes too.
    """
    header = _FEATURES_HEADER.search(text)
    if header is None:
        return text
    rest = text[header.end():]
    nxt = _TABLE_HEADER.search(rest)
    section, tail = (rest[:nxt.start()], rest[nxt.start():]) if nxt else (rest, "")
    new_section, n = _ATTRIB_LINE_FALSE.subn("", section, count=1)
    if n == 0:
        return text
    if not new_section.strip():
        prefix = text[:header.start()].rstrip("\n")
        if not tail.strip():
            return prefix + "\n" if prefix else ""
        return (prefix + "\n\n" if prefix else "") + tail.lstrip("\n")
    return text[:header.end()] + new_section + tail


def codex_extract_unmanaged(text: str) -> str:
    """Concatenation of the ``[projects]`` table sections, verbatim ("" if none)."""
    parts = [text[s:e].strip("\n") + "\n"
             for s, e, name in _codex_sections(text) if _is_projects(name)]
    return "\n".join(parts)


def codex_strip_unmanaged(text: str) -> str:
    """Remove every ``[projects]`` / ``[projects."…"]`` section, rest verbatim."""
    spans = [(s, e) for s, e, name in _codex_sections(text) if _is_projects(name)]
    if not spans:
        return text
    for s, e in reversed(spans):
        text = text[:s] + text[e:]
    text = re.sub(r"\n{3,}", "\n\n", text)
    body = text.rstrip("\n")
    return body + "\n" if body else ""


def codex_merge_unmanaged(live_text: str, content: str) -> str:
    """Give content the live config's projects tables; profile copies never win."""
    content = codex_strip_unmanaged(content)
    extra = codex_extract_unmanaged(live_text)
    if not extra:
        return content
    body = content.rstrip("\n")
    return (body + "\n\n" if body else "") + extra


def codex_is_empty(text: str) -> bool:
    return not text.strip()


def _claude_obj(text: str) -> "dict | None":
    try:
        obj = json.loads(text)
    except json.JSONDecodeError:
        return None
    return obj if isinstance(obj, dict) else None


def _claude_dump(obj: dict) -> str:
    return json.dumps(obj, indent=2, ensure_ascii=False) + "\n"


def claude_extract_unmanaged(text: str) -> str:
    """JSON text holding only the unmanaged keys ("" if none present)."""
    obj = _claude_obj(text)
    if not obj:
        return ""
    kept = {k: obj[k] for k in CLAUDE_UNMANAGED_KEYS if k in obj}
    return _claude_dump(kept) if kept else ""


def claude_strip_unmanaged(text: str) -> str:
    obj = _claude_obj(text)
    if not obj or not any(k in obj for k in CLAUDE_UNMANAGED_KEYS):
        return text
    for k in CLAUDE_UNMANAGED_KEYS:
        obj.pop(k, None)
    return _claude_dump(obj)


def claude_merge_unmanaged(live_text: str, content: str) -> str:
    """Give content the live config's unmanaged keys; profile copies never win."""
    src = _claude_obj(live_text) or {}
    taken = {k: src[k] for k in CLAUDE_UNMANAGED_KEYS if k in src}
    cobj = _claude_obj(content)
    if cobj is None:
        cobj = {}
    if not taken and not any(k in cobj for k in CLAUDE_UNMANAGED_KEYS):
        return content
    for k in CLAUDE_UNMANAGED_KEYS:
        cobj.pop(k, None)
    cobj.update(taken)
    return _claude_dump(cobj)


def claude_is_empty(text: str) -> bool:
    return not _claude_obj(text)  # unparsable counts as empty; callers validated first


def claude_hide_attribution(text: str) -> str:
    """Hide Claude's commit attribution in settings.json content.

    Sets both spellings: ``includeCoAuthoredBy: false`` (older Claude Code)
    and ``attribution.commit: ""`` (Claude Code >= 2.1, where an empty string
    hides the footer *and* the Co-Authored-By trailer). Existing keys keep
    their position; only formatting is normalised.
    """
    try:
        obj = json.loads(text)
    except json.JSONDecodeError:
        return text  # invalid; `use` refuses to switch on validate anyway
    if not isinstance(obj, dict):
        obj = {}
    obj["includeCoAuthoredBy"] = False
    attr = obj.get("attribution")
    if not isinstance(attr, dict):
        attr = {}
    attr["commit"] = ""
    obj["attribution"] = attr
    return json.dumps(obj, indent=2, ensure_ascii=False) + "\n"


def claude_strip_attribution(text: str) -> str:
    """Remove the attribution keys ais injects; user customisations stay."""
    try:
        obj = json.loads(text)
    except json.JSONDecodeError:
        return text
    if not isinstance(obj, dict):
        return text
    changed = False
    if obj.get("includeCoAuthoredBy") is False:
        del obj["includeCoAuthoredBy"]
        changed = True
    attr = obj.get("attribution")
    if isinstance(attr, dict) and attr.get("commit") == "":
        del attr["commit"]
        if not attr:
            del obj["attribution"]
        changed = True
    if not changed:
        return text
    return json.dumps(obj, indent=2, ensure_ascii=False) + "\n"


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
    validate=validate_toml,
    auth_files=("auth.json",),
    prepare_use=codex_prepare_use,
    post_save=codex_post_save,
    hide_attribution=codex_hide_attribution,
    strip_attribution=codex_strip_attribution,
    empty_content="",
    unmanaged_desc="directory trust ([projects] tables)",
    extract_unmanaged=codex_extract_unmanaged,
    strip_unmanaged=codex_strip_unmanaged,
    merge_unmanaged=codex_merge_unmanaged,
    is_empty=codex_is_empty,
)

CLAUDE = App(
    name="claude",
    live_dir_name=".claude",
    live_file="settings.json",
    binary="claude",
    validate=validate_json,
    auth_files=(".credentials.json",),
    prepare_use=_identity_prepare,
    post_save=_no_post_save,
    hide_attribution=claude_hide_attribution,
    strip_attribution=claude_strip_attribution,
    empty_content="{}",
    unmanaged_desc="permissions (incl. additionalDirectories)",
    extract_unmanaged=claude_extract_unmanaged,
    strip_unmanaged=claude_strip_unmanaged,
    merge_unmanaged=claude_merge_unmanaged,
    is_empty=claude_is_empty,
)

APPS = {"codex": CODEX, "claude": CLAUDE}
APPS_ORDER = (CODEX, CLAUDE)
