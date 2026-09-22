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


def test_use_claude_preserves_fields_and_hides_attribution(home):
    make_claude_profile(home, "p1")
    r = run("claude", "use", "p1")
    assert r.exit_code == 0, r.output
    live = json.loads((home / ".claude" / "settings.json").read_text())
    expected = json.loads(CLAUDE_SETTINGS)
    expected["includeCoAuthoredBy"] = False
    expected["attribution"] = {"commit": ""}
    assert live == expected
    assert "hiding AI commit attribution" in r.output
    assert "p1" in run("claude", "current").output


def test_use_claude_verbatim_when_attribution_off(home):
    make_claude_profile(home, "p1")
    assert run("attribution", "off").exit_code == 0
    r = run("claude", "use", "p1")
    assert r.exit_code == 0, r.output
    assert (home / ".claude" / "settings.json").read_text() == CLAUDE_SETTINGS
    assert "hiding AI commit attribution" not in r.output


def test_use_unknown_profile_fails(home):
    assert run("codex", "use", "nope").exit_code != 0


# ------------------------------------------------------------------ hide AI attribution

def test_attribution_defaults_on_and_toggles(home):
    assert "hide AI commit attribution: on" in run("attribution").output
    assert run("attribution", "off").exit_code == 0
    assert "hide AI commit attribution: off" in run("attribution").output
    settings = json.loads((home / ".config" / "ais" / "settings.json").read_text())
    assert settings == {"hide_ai_attribution": False}
    assert run("attribution", "on").exit_code == 0
    settings = json.loads((home / ".config" / "ais" / "settings.json").read_text())
    assert settings == {"hide_ai_attribution": True}
    assert run("attribution", "bogus").exit_code != 0


def test_status_shows_attribution_state(home):
    assert "hide AI commit attribution: on" in run("status").output
    run("attribution", "off")
    assert "hide AI commit attribution: off" in run("status").output


def test_use_codex_hides_attribution_default_on(home):
    make_codex_profile(home, "p1", text='model = "m"\nmodel_provider = "x"\n')
    r = run("codex", "use", "p1")
    assert r.exit_code == 0, r.output
    live = (home / ".codex" / "config.toml").read_text()
    assert "[features]\ncommit_attribution_enabled = false" in live
    assert tomllib.loads(live)["features"]["commit_attribution_enabled"] is False
    assert "hiding AI commit attribution" in r.output


def test_use_codex_attribution_joins_existing_features_table(home):
    text = ('model = "m"\n'
            '# keep me\n'
            '\n'
            '[features]\n'
            'other_feature = true  # inline comment stays\n'
            '\n'
            '[model_providers.x]\n'
            'base_url = "https://example.com/v1"\n')
    make_codex_profile(home, "p1", text=text)
    assert run("codex", "use", "p1").exit_code == 0
    live = (home / ".codex" / "config.toml").read_text()
    assert live.count("[features]") == 1
    parsed = tomllib.loads(live)
    assert parsed["features"] == {"commit_attribution_enabled": False,
                                  "other_feature": True}
    assert "# keep me" in live
    assert "# inline comment stays" in live


def test_use_codex_attribution_overrides_explicit_true(home):
    make_codex_profile(home, "p1", text='[features]\ncommit_attribution_enabled = true\n')
    assert run("codex", "use", "p1").exit_code == 0
    live = (home / ".codex" / "config.toml").read_text()
    assert tomllib.loads(live)["features"]["commit_attribution_enabled"] is False


def test_use_codex_attribution_off_writes_verbatim(home):
    make_codex_profile(home, "p1", text='model = "m"\n')
    run("attribution", "off")
    r = run("codex", "use", "p1")
    assert r.exit_code == 0
    assert (home / ".codex" / "config.toml").read_text() == 'model = "m"\n'
    assert "hiding AI commit attribution" not in r.output


def test_save_strips_injected_attribution(home):
    # codex: live has ais-injected key -> profile stays portable
    make_codex_profile(home, "src", text='model = "m"\n')
    assert run("codex", "use", "src").exit_code == 0
    assert "commit_attribution_enabled" in (home / ".codex" / "config.toml").read_text()
    assert run("codex", "save", "clean").exit_code == 0
    saved = (home / ".config" / "ais" / "codex" / "clean" / "config.toml").read_text()
    assert "commit_attribution_enabled" not in saved
    assert "[features]" not in saved
    assert saved == 'model = "m"\n'

    # claude: same round-trip
    make_claude_profile(home, "src")
    assert run("claude", "use", "src").exit_code == 0
    assert run("claude", "save", "clean").exit_code == 0
    saved = json.loads((home / ".config" / "ais" / "claude" / "clean" / "settings.json").read_text())
    assert saved == json.loads(CLAUDE_SETTINGS)
    assert "includeCoAuthoredBy" not in saved and "attribution" not in saved


def test_save_keeps_user_written_attribution(home):
    live = home / ".claude" / "settings.json"
    live.write_text(json.dumps({"attribution": {"commit": "", "pr": "custom"},
                                "includeCoAuthoredBy": True}, indent=2))
    assert run("claude", "save", "p").exit_code == 0
    saved = json.loads((home / ".config" / "ais" / "claude" / "p" / "settings.json").read_text())
    assert saved == {"attribution": {"pr": "custom"}, "includeCoAuthoredBy": True}

    live2 = home / ".codex" / "config.toml"
    live2.write_text('[features]\ncommit_attribution_enabled = true\n')
    assert run("codex", "save", "q").exit_code == 0
    saved2 = (home / ".config" / "ais" / "codex" / "q" / "config.toml").read_text()
    assert "commit_attribution_enabled = true" in saved2


def test_doctor_catches_broken_settings_json(home):
    sdir = home / ".config" / "ais"
    sdir.mkdir(parents=True)
    (sdir / "settings.json").write_text("{broken")
    assert run("doctor").exit_code == 1
    assert "settings.json" in run("validate").output


# ------------------------------------------------------------------ clear / official

def test_clear_removes_overrides_keeps_auth_and_hides_attribution(home):
    auth = home / ".codex" / "auth.json"
    auth.write_text('{"tokens": "keep-me"}')
    make_codex_profile(home, "p1")
    assert run("codex", "use", "p1").exit_code == 0
    assert (home / ".codex" / "config.toml").is_file()

    r = run("codex", "clear")
    assert r.exit_code == 0, r.output
    # minimal config holding only the hide keys, provider overrides gone
    live = (home / ".codex" / "config.toml").read_text()
    assert tomllib.loads(live) == {"features": {"commit_attribution_enabled": False}}
    assert "hiding AI commit attribution" in r.output
    assert auth.read_text() == '{"tokens": "keep-me"}'
    assert run("codex", "current").output.strip() == "official"
    # the replaced config was backed up first
    backups = list((home / ".config" / "ais" / "backups" / "codex").iterdir())
    assert len(backups) == 1


def test_clear_without_attribution_hiding_deletes_live_config(home):
    make_codex_profile(home, "p1")
    assert run("codex", "use", "p1").exit_code == 0
    run("attribution", "off")
    r = run("codex", "clear")
    assert r.exit_code == 0, r.output
    assert not (home / ".codex" / "config.toml").exists()
    assert run("codex", "current").output.strip() == "official"


def test_clear_official_state_detects_manual_modification(home):
    make_codex_profile(home, "p1")
    assert run("codex", "use", "p1").exit_code == 0
    assert run("codex", "clear").exit_code == 0
    assert run("codex", "current").output.strip() == "official"
    (home / ".codex" / "config.toml").write_text('model = "manual"\n')
    assert run("codex", "current").output.strip() == "unmanaged"


def test_clear_claude_writes_minimal_hidden_settings(home):
    make_claude_profile(home, "p1")
    assert run("claude", "use", "p1").exit_code == 0
    assert run("claude", "clear").exit_code == 0
    settings = json.loads((home / ".claude" / "settings.json").read_text())
    assert settings == {"includeCoAuthoredBy": False, "attribution": {"commit": ""}}
    assert run("claude", "current").output.strip() == "official"


def test_clear_rejects_removed_hard_option(home):
    assert run("codex", "clear", "--hard").exit_code != 0


def test_official_writes_minimal_hidden_config(home):
    live = home / ".codex" / "config.toml"
    live.write_text('model = "orig"\n')
    make_codex_profile(home, "p1")
    assert run("codex", "use", "p1").exit_code == 0

    r = run("codex", "official")
    assert r.exit_code == 0, r.output
    assert tomllib.loads(live.read_text()) == {"features": {"commit_attribution_enabled": False}}
    assert run("codex", "current").output.strip() == "official"

    # `use official` is the same thing
    assert run("codex", "use", "p1").exit_code == 0
    r = run("codex", "use", "official")
    assert r.exit_code == 0, r.output
    assert tomllib.loads(live.read_text()) == {"features": {"commit_attribution_enabled": False}}


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
