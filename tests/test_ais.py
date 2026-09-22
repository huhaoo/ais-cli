"""Tests for ais. All filesystem access is redirected via AIS_HOME."""

import json
import os
import tomllib
from pathlib import Path

import pytest
from typer.testing import CliRunner

from ais.cli import app as cli_app

runner = CliRunner()

MODELS_JSON = json.dumps({
    "gpt-5.6-luna": {
        "display_name": "Luna",
        "supported_reasoning_levels": ["low", "medium", "high"],
        "default_reasoning_level": "medium",
        "context_window": 400000,
        "max_context_window": 400000,
        "input_modalities": ["text", "image"],
        "unknown_future_field": {"nested": [1, 2, {"x": None}]},
    },
    "gpt-6-astra": {"display_name": "Astra", "context_window": 1000000},
}, indent=2)

CLAUDE_SETTINGS = json.dumps({
    "env": {
        "ANTHROPIC_BASE_URL": "https://example.com/api",
        "ANTHROPIC_AUTH_TOKEN": "sk-ant-secret",
        "ANTHROPIC_MODEL": "claude-sonnet-5",
        "ANTHROPIC_DEFAULT_HAIKU_MODEL": "claude-haiku-4-5",
        "ANTHROPIC_DEFAULT_SONNET_MODEL": "claude-sonnet-5",
        "ANTHROPIC_DEFAULT_OPUS_MODEL": "claude-opus-5",
    },
    "permissions": {"allow": ["Bash(ls:*)"], "futureUnknownField": {"a": 1}},
    "totally_new_field": [1, 2, 3],
}, indent=2)


def run(*args):
    return runner.invoke(cli_app, list(args))


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("AIS_HOME", str(tmp_path))
    (tmp_path / ".codex").mkdir()
    (tmp_path / ".claude").mkdir()
    return tmp_path


def make_codex_profile(home, name, with_models=False, text=None):
    pd = home / ".config" / "ais" / "codex" / name
    pd.mkdir(parents=True)
    (pd / "config.toml").write_text(
        text if text is not None else
        'model = "gpt-5.6-luna"\n'
        'model_provider = "custom"\n'
        'model_catalog_json = "models.json"\n'
        '\n'
        '[model_providers.custom]\n'
        'name = "example"\n'
        'base_url = "https://example.com/v1"\n'
        'wire_api = "responses"\n'
        'experimental_bearer_token = "sk-secret"\n'
    )
    if with_models:
        (pd / "models.json").write_text(MODELS_JSON)
    return pd


def make_claude_profile(home, name):
    pd = home / ".config" / "ais" / "claude" / name
    pd.mkdir(parents=True)
    (pd / "settings.json").write_text(CLAUDE_SETTINGS)
    return pd


# ------------------------------------------------------------------ fresh start

def test_empty_first_run(home):
    for args in (["codex", "list"], ["claude", "list"]):
        r = run(*args)
        assert r.exit_code == 0
        assert "No profiles configured." in r.output
    assert "unmanaged" in run("codex", "current").output
    assert "unmanaged" in run("claude", "current").output
    r = run("status")
    assert r.exit_code == 0
    assert "official auth: not detected" in r.output


def test_no_auto_created_profiles(home):
    run("codex", "list")
    run("codex", "current")
    run("claude", "list")
    run("status")
    run("doctor")
    for sub in ("codex", "claude"):
        pdir = home / ".config" / "ais" / sub
        assert not pdir.exists() or not any(pdir.iterdir())


# ------------------------------------------------------------------ save

def test_save_codex_with_models_json(home):
    catalog = home / ".codex" / "models.json"
    catalog.write_text(MODELS_JSON)
    (home / ".codex" / "auth.json").write_text('{"OPENAI_API_KEY": "secret"}')
    live = home / ".codex" / "config.toml"
    original = (f'model = "gpt-5.6-luna"\n'
                f'model_catalog_json = "{catalog}"\n'
                f'\n'
                f'[model_providers.custom]\n'
                f'base_url = "https://example.com/v1"\n')
    live.write_text(original)

    r = run("codex", "save", "work")
    assert r.exit_code == 0, r.output

    pd = home / ".config" / "ais" / "codex" / "work"
    assert (pd / "models.json").read_text() == MODELS_JSON  # verbatim copy
    cfg = tomllib.loads((pd / "config.toml").read_text())
    assert cfg["model_catalog_json"] == "models.json"  # portable placeholder
    assert not (pd / "auth.json").exists()  # never copy official login
    assert live.read_text() == original  # live untouched
    # save does not modify live config
    assert not (home / ".config" / "ais" / "backups" / "codex").exists()


def test_save_codex_keeps_original_catalog_path_when_missing(home):
    (home / ".codex" / "config.toml").write_text(
        'model_catalog_json = "/nonexistent/models.json"\n')
    r = run("codex", "save", "x")
    assert r.exit_code == 0
    pd = home / ".config" / "ais" / "codex" / "x"
    assert not (pd / "models.json").exists()
    assert tomllib.loads((pd / "config.toml").read_text())["model_catalog_json"] \
        == "/nonexistent/models.json"


def test_save_claude_preserves_unknown_fields(home):
    live = home / ".claude" / "settings.json"
    live.write_text(CLAUDE_SETTINGS)
    r = run("claude", "save", "work")
    assert r.exit_code == 0, r.output
    saved = home / ".config" / "ais" / "claude" / "work" / "settings.json"
    assert saved.read_text() == CLAUDE_SETTINGS  # byte-identical
    assert not (home / ".config" / "ais" / "claude" / "work" / ".credentials.json").exists()


def test_save_refuses_overwrite_without_force(home):
    (home / ".claude" / "settings.json").write_text(CLAUDE_SETTINGS)
    assert run("claude", "save", "work").exit_code == 0
    (home / ".claude" / "settings.json").write_text('{"env": {}}\n')
    r = run("claude", "save", "work")
    assert r.exit_code != 0
    assert (home / ".config" / "ais" / "claude" / "work" / "settings.json").read_text() \
        == CLAUDE_SETTINGS  # unchanged
    r = run("claude", "save", "work", "--force")
    assert r.exit_code == 0
    assert (home / ".config" / "ais" / "claude" / "work" / "settings.json").read_text() \
        == '{"env": {}}\n'


def test_save_requires_live_config(home):
    r = run("codex", "save", "nothing")
    assert r.exit_code != 0
    assert "no live config" in r.output


# ------------------------------------------------------------------ use

def test_use_codex_rewrites_catalog_to_profile_models(home):
    pd = make_codex_profile(home, "p1", with_models=True)
    r = run("codex", "use", "p1")
    assert r.exit_code == 0, r.output

    live = tomllib.loads((home / ".codex" / "config.toml").read_text())
    assert live["model_catalog_json"] == str((pd / "models.json").resolve())
    # the file the live config points at is byte-identical, unknown fields intact
    pointed = Path(live["model_catalog_json"])
    assert json.loads(pointed.read_text()) == json.loads(MODELS_JSON)
    assert live["model_providers"]["custom"]["experimental_bearer_token"] == "sk-secret"

    assert "p1" in run("codex", "current").output


def test_use_codex_without_models_json_leaves_paths_alone(home):
    make_codex_profile(home, "plain", with_models=False,
                       text='model_catalog_json = "/keep/me.json"\nmodel = "m"\n')
    assert run("codex", "use", "plain").exit_code == 0
    live = tomllib.loads((home / ".codex" / "config.toml").read_text())
    assert live["model_catalog_json"] == "/keep/me.json"


def test_use_claude_verbatim(home):
    make_claude_profile(home, "p1")
    r = run("claude", "use", "p1")
    assert r.exit_code == 0, r.output
    assert (home / ".claude" / "settings.json").read_text() == CLAUDE_SETTINGS
    assert "p1" in run("claude", "current").output


def test_use_unknown_profile_fails(home):
    assert run("codex", "use", "nope").exit_code != 0


# ------------------------------------------------------------------ clear / official

def test_clear_deletes_live_config_and_keeps_auth(home):
    auth = home / ".codex" / "auth.json"
    auth.write_text('{"tokens": "keep-me"}')
    make_codex_profile(home, "p1")
    assert run("codex", "use", "p1").exit_code == 0
    assert (home / ".codex" / "config.toml").is_file()

    r = run("codex", "clear")
    assert r.exit_code == 0, r.output
    assert not (home / ".codex" / "config.toml").exists()
    assert auth.read_text() == '{"tokens": "keep-me"}'
    assert run("codex", "current").output.strip() == "official"
    # the removed config was backed up first
    backups = list((home / ".config" / "ais" / "backups" / "codex").iterdir())
    assert len(backups) == 1


def test_clear_rejects_removed_hard_option(home):
    assert run("codex", "clear", "--hard").exit_code != 0


def test_official_deletes_live_config(home):
    live = home / ".codex" / "config.toml"
    live.write_text('model = "orig"\n')
    make_codex_profile(home, "p1")
    assert run("codex", "use", "p1").exit_code == 0

    r = run("codex", "official")
    assert r.exit_code == 0, r.output
    assert not live.exists()
    assert run("codex", "current").output.strip() == "official"

    # `use official` is the same thing
    assert run("codex", "use", "p1").exit_code == 0
    r = run("codex", "use", "official")
    assert r.exit_code == 0, r.output
    assert not live.exists()


def test_reset_baseline_command_removed(home):
    assert run("codex", "reset-baseline").exit_code != 0


# ------------------------------------------------------------------ safety

def test_invalid_toml_profile_never_touches_live(home):
    (home / ".codex" / "config.toml").write_text('valid = true\n')
    make_codex_profile(home, "bad",
                       text='this is [ not valid toml\n')
    r = run("codex", "use", "bad")
    assert r.exit_code != 0
    assert (home / ".codex" / "config.toml").read_text() == 'valid = true\n'


def test_invalid_json_profile_never_touches_live(home):
    (home / ".claude" / "settings.json").write_text('{"valid": true}\n')
    pd = home / ".config" / "ais" / "claude" / "bad"
    pd.mkdir(parents=True)
    (pd / "settings.json").write_text('{"unclosed": ')
    r = run("claude", "use", "bad")
    assert r.exit_code != 0
    assert (home / ".claude" / "settings.json").read_text() == '{"valid": true}\n'


def test_doctor_and_validate_catch_bad_profiles(home):
    (home / ".claude" / "settings.json").write_text('{}\n')
    make_claude_profile(home, "good")
    pd = home / ".config" / "ais" / "claude" / "bad"
    pd.mkdir(parents=True)
    (pd / "settings.json").write_text('nope')
    assert run("validate").exit_code == 1
    assert run("doctor").exit_code == 1
    (pd / "settings.json").write_text('{}')
    assert run("validate").exit_code == 0
    assert run("doctor").exit_code == 0


# ------------------------------------------------------------------ current detection

def test_current_detects_manual_modification(home):
    make_codex_profile(home, "p1")
    make_claude_profile(home, "p1")
    assert run("codex", "use", "p1").exit_code == 0
    assert run("claude", "use", "p1").exit_code == 0

    out = run("codex", "current").output.strip()
    assert out == "p1"

    (home / ".codex" / "config.toml").write_text('model = "hand-edited"\n')
    assert run("codex", "current").output.strip() == "p1 (modified)"
    # claude unaffected
    assert run("claude", "current").output.strip() == "p1"

    (home / ".codex" / "config.toml").unlink()
    assert run("codex", "current").output.strip() == "p1 (missing)"


# ------------------------------------------------------------------ misc

def test_backup_command(home):
    (home / ".codex" / "config.toml").write_text('a = 1\n')
    r = run("backup")
    assert r.exit_code == 0
    assert len(list((home / ".config" / "ais" / "backups" / "codex").iterdir())) == 1


def test_delete_protects_current_profile(home):
    make_claude_profile(home, "p1")
    assert run("claude", "use", "p1").exit_code == 0
    assert run("claude", "delete", "p1").exit_code != 0
    assert (home / ".config" / "ais" / "claude" / "p1").is_dir()
    assert run("claude", "delete", "p1", "--force").exit_code == 0
    assert not (home / ".config" / "ais" / "claude" / "p1").exists()


def test_run_applies_env_json(home, monkeypatch):
    pd = make_codex_profile(home, "p1")
    (pd / "env.json").write_text('{"AIS_MARK": "hello"}')
    bindir = home / "bin"
    bindir.mkdir()
    script = bindir / "codex"
    script.write_text('#!/bin/sh\nprintf "%s" "$AIS_MARK" > "$AIS_OUT"\n')
    script.chmod(0o755)
    monkeypatch.setenv("PATH", str(bindir))
    monkeypatch.setenv("AIS_OUT", str(home / "out"))
    r = run("codex", "run", "p1")
    assert r.exit_code == 0, r.output
    assert (home / "out").read_text() == "hello"
    # profile stays active after run (no auto-switch-back)
    assert run("codex", "current").output.strip() == "p1"


def test_invalid_profile_name_rejected(home):
    assert run("codex", "use", "../evil").exit_code != 0
    assert run("codex", "save", "a/b").exit_code != 0


def test_state_json_shape(home):
    make_codex_profile(home, "p1")
    assert run("codex", "use", "p1").exit_code == 0
    state = json.loads((home / ".config" / "ais" / "state.json").read_text())
    assert state["codex"]["current"] == "p1"
    assert isinstance(state["codex"]["hash"], str)


# ------------------------------------------------------------------ completion

def test_shell_completion_dynamic_profiles(home):
    from click.shell_completion import ShellComplete
    from typer.main import get_command

    make_codex_profile(home, "p1")
    make_codex_profile(home, "p2")
    make_claude_profile(home, "q1")

    sc = ShellComplete(get_command(cli_app), {}, "ais", "_AIS_COMPLETE")
    root = sorted(i.value for i in sc.get_completions([], ""))
    assert {"codex", "claude", "status", "doctor"} <= set(root)

    codex_cmds = sorted(i.value for i in sc.get_completions(["codex"], ""))
    assert {"use", "save", "clear", "official", "run"} <= set(codex_cmds)

    profiles = sorted(i.value for i in sc.get_completions(["codex", "use"], ""))
    assert profiles == ["official", "p1", "p2"]  # dynamic + 'official', no claude names

    prefix = sorted(i.value for i in sc.get_completions(["codex", "use"], "p"))
    assert prefix == ["p1", "p2"]

    claude_profiles = sorted(i.value for i in sc.get_completions(["claude", "use"], ""))
    assert claude_profiles == ["official", "q1"]
