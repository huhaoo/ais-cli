"""ais command-line interface."""

from __future__ import annotations

import json
import os
import shutil
import subprocess

import typer

from . import __version__, core
from . import sync as sync_mod
from . import usage as usage_mod
from .apps import APPS_ORDER
from .core import App

app = typer.Typer(
    help="ais — switch provider profiles for OpenAI Codex CLI and Claude Code.\n\n"
         "Plain-text config only. No database, no daemon, no traffic proxying.",
    no_args_is_help=True,
)


def _die(msg: str):
    typer.secho(f"error: {msg}", fg=typer.colors.RED, err=True)
    raise typer.Exit(1)


def _warn(msg: str):
    typer.secho(f"warning: {msg}", fg=typer.colors.YELLOW, err=True)


def _auth_note(app_spec: App) -> str:
    return ", ".join(f"~/{app_spec.live_dir_name}/{f}" for f in app_spec.auth_files)


def _complete_profiles(a: App):
    """Typer autocompletion callback: existing profile names plus 'official'.

    Typer filters candidates by the incomplete prefix itself.
    """
    def complete(ctx, args, incomplete: str):
        try:
            names = core.list_profiles(core.get_home(), a)
        except Exception:
            names = []
        return [(n, "profile") for n in sorted(set(names))] + \
               [("official", "remove overrides, use official login")]
    return complete


# ---------------------------------------------------------------- command logic

def _cmd_use(a: App, profile: str) -> None:
    home = core.get_home()
    if profile == "official":
        _cmd_clear(a)
        return
    core.check_name(profile)
    pdir = core.profile_dir(home, a, profile)
    if not core.profile_exists(home, a, profile):
        _die(f"profile '{profile}' not found (see: ais {a.name} list)")
    content = (pdir / a.live_file).read_text(encoding="utf-8")
    try:
        a.validate(content)
    except Exception as e:
        _die(f"profile '{profile}' {a.live_file} is invalid, refusing to switch: {e}")
    content = a.prepare_use(pdir, content)
    if core.hide_ai_attribution(home):
        hidden = a.hide_attribution(content)
        if hidden != content:
            content = hidden
            typer.echo("note: hiding AI commit attribution "
                       "('ais attribution off' to keep it)")
    try:
        a.validate(content)
    except Exception as e:
        _die(f"generated config for '{profile}' is invalid, refusing to switch: {e}")

    backup = core.backup_live(home, a)
    core.atomic_write(core.live_path(home, a), content)
    core.set_state(home, a, current=profile, live_hash=core.sha256_text(content))
    typer.echo(f"{a.name}: switched to profile '{profile}'")
    if backup:
        typer.echo(f"backup: {backup}")
    if (pdir / "env.json").is_file():
        typer.echo("note: profile has env.json; it is applied by "
                   f"'ais {a.name} run {profile}', not by 'use'")


def _cmd_save(a: App, profile: str, force: bool) -> None:
    home = core.get_home()
    core.check_name(profile)
    pdir = core.profile_dir(home, a, profile)
    if pdir.exists():
        if not force:
            _die(f"profile '{profile}' already exists; use --force to overwrite")
        shutil.rmtree(pdir)
    live = core.live_path(home, a)
    if not live.is_file():
        _die(f"no live config to save at {live}")
    text = live.read_text(encoding="utf-8")
    try:
        a.validate(text)
    except Exception as e:
        _die(f"live {a.live_file} is invalid, nothing saved: {e}")
    text = a.strip_attribution(text)  # profiles stay free of ais-injected keys
    try:
        a.validate(text)
    except Exception as e:
        _die(f"stripped config for '{profile}' is invalid, nothing saved: {e}")

    pdir.mkdir(parents=True, exist_ok=True)
    core.atomic_write(pdir / a.live_file, text)
    for w in a.post_save(a, home, pdir):
        _warn(w)
    typer.echo(f"{a.name}: saved live config as profile '{profile}' ({pdir})")
    typer.echo(f"official login files are never copied: {_auth_note(a)}")


def _cmd_clear(a: App) -> None:
    """Remove provider overrides so the app falls back to its official login.

    With attribution hiding on (the default), a minimal live config holding
    only the hide keys is written instead of deleting the file, so commits
    stay attribution-free on the official login too.
    """
    home = core.get_home()
    live = core.live_path(home, a)
    backup = core.backup_live(home, a)

    hidden = None
    if core.hide_ai_attribution(home):
        hidden = a.hide_attribution(a.empty_content)
        try:
            a.validate(hidden)
        except Exception as e:
            _die(f"generated minimal config is invalid, refusing to write: {e}")

    if hidden is not None:
        core.atomic_write(live, hidden)
        core.set_state(home, a, current=None, live_hash=core.sha256_text(hidden),
                       official=True)
        typer.echo(f"{a.name}: provider overrides removed (official login takes over)")
        typer.echo(f"{a.name}: wrote minimal config hiding AI commit attribution "
                   f"at {live}")
    else:
        if live.exists():
            live.unlink()
            typer.echo(f"{a.name}: removed {live} (official login takes over)")
        else:
            typer.echo(f"{a.name}: no live config at {live}")
        core.set_state(home, a, current=None, live_hash=None, official=True)

    if backup:
        typer.echo(f"backup: {backup}")
    typer.echo(f"official login untouched: {_auth_note(a)}")


def _cmd_run(a: App, profile: str) -> None:
    home = core.get_home()
    if profile == "official":
        _cmd_clear(a)
    else:
        _cmd_use(a, profile)
    env = dict(os.environ)
    if profile != "official":
        envf = core.profile_dir(home, a, profile) / "env.json"
        if envf.is_file():
            try:
                data = json.loads(envf.read_text(encoding="utf-8"))
            except json.JSONDecodeError as e:
                _die(f"env.json in profile '{profile}' is invalid JSON: {e}")
            if not (isinstance(data, dict)
                    and all(isinstance(k, str) and isinstance(v, str)
                            for k, v in data.items())):
                _die("env.json must be a JSON object mapping names to strings")
            env.update(data)
    try:
        rc = subprocess.call([a.binary], env=env)
    except FileNotFoundError:
        _die(f"command '{a.binary}' not found in PATH")
    raise typer.Exit(rc)


def _cmd_delete(a: App, profile: str, force: bool) -> None:
    home = core.get_home()
    core.check_name(profile)
    pdir = core.profile_dir(home, a, profile)
    if not pdir.is_dir():
        _die(f"profile '{profile}' not found")
    cur = core.state_entry(home, a).get("current")
    if cur == profile and not force:
        _die(f"profile '{profile}' is currently in use; run 'ais {a.name} clear' "
             f"first, or pass --force")
    shutil.rmtree(pdir)
    if cur == profile:
        core.set_state(home, a, current=None, live_hash=None)
        _warn("deleted the active profile; the live config was left as-is")
    typer.echo(f"{a.name}: deleted profile '{profile}'")


def _cmd_show(a: App, profile: str) -> None:
    home = core.get_home()
    core.check_name(profile)
    pdir = core.profile_dir(home, a, profile)
    if not core.profile_exists(home, a, profile):
        _die(f"profile '{profile}' not found")
    main = pdir / a.live_file
    typer.echo(f"# {main}")
    typer.echo(main.read_text(encoding="utf-8").rstrip("\n"))
    extras = sorted(p.name for p in pdir.iterdir()
                    if p.is_file() and p.name != a.live_file)
    if extras:
        typer.echo(f"\n# additional files in profile: {', '.join(extras)}")


def _cmd_edit(a: App, profile: str) -> None:
    home = core.get_home()
    core.check_name(profile)
    if not core.profile_exists(home, a, profile):
        _die(f"profile '{profile}' not found")
    target = core.profile_dir(home, a, profile) / a.live_file
    editor = os.environ.get("EDITOR") or "vi"
    try:
        rc = subprocess.call([editor, str(target)])
    except FileNotFoundError:
        _die(f"editor '{editor}' not found (set $EDITOR)")
    if rc != 0:
        raise typer.Exit(rc)
    try:
        a.validate(target.read_text(encoding="utf-8"))
        typer.echo(f"{a.live_file} is valid")
    except Exception as e:
        _warn(f"profile now fails validation: {e}")


# ---------------------------------------------------------------- checks

def _check_all(home) -> "list[str]":
    problems: "list[str]" = []
    try:
        core.ais_dir(home).mkdir(parents=True, exist_ok=True)
    except OSError as e:
        return [f"cannot create {core.ais_dir(home)}: {e}"]

    sp = core.state_path(home)
    if sp.is_file():
        try:
            json.loads(sp.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            problems.append(f"state.json: {e}")

    sp = core.settings_path(home)
    if sp.is_file():
        try:
            data = json.loads(sp.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError("expected a JSON object")
        except (OSError, ValueError) as e:
            problems.append(f"settings.json: {e}")

    for a in APPS_ORDER:
        live = core.live_path(home, a)
        if live.is_file():
            try:
                a.validate(live.read_text(encoding="utf-8"))
            except Exception as e:
                problems.append(f"live {a.name} config ({live}): {e}")

        for name in core.list_profiles(home, a):
            pdir = core.profile_dir(home, a, name)
            try:
                a.validate((pdir / a.live_file).read_text(encoding="utf-8"))
            except Exception as e:
                problems.append(f"profile {a.name}/{name}: {a.live_file}: {e}")
            for extra in ("models.json", "env.json"):
                p = pdir / extra
                if not p.is_file():
                    continue
                try:
                    data = json.loads(p.read_text(encoding="utf-8"))
                    if extra == "env.json" and not (
                        isinstance(data, dict)
                        and all(isinstance(k, str) and isinstance(v, str)
                                for k, v in data.items())
                    ):
                        raise ValueError("must map names to strings")
                except Exception as e:
                    problems.append(f"profile {a.name}/{name}: {extra}: {e}")
    return problems


# ---------------------------------------------------------------- per-app subcommands

def _make_subapp(a: App) -> typer.Typer:
    t = typer.Typer(help=f"Manage {a.name} provider profiles.",
                    no_args_is_help=True)

    @t.command("list")
    def list_cmd() -> None:
        """List saved profiles."""
        home = core.get_home()
        names = core.list_profiles(home, a)
        if not names:
            typer.echo("No profiles configured.")
            return
        cur = core.state_entry(home, a).get("current")
        for n in names:
            typer.echo(f"{n}{'  (current)' if n == cur else ''}")

    @t.command("current")
    def current_cmd() -> None:
        """Show which profile is currently in use."""
        typer.echo(core.resolve_current(core.get_home(), a))

    @t.command("use")
    def use_cmd(
        profile: str = typer.Argument(..., help="Profile name, or 'official'.",
                                      autocompletion=_complete_profiles(a)),
    ) -> None:
        """Switch the live config to a profile."""
        _cmd_use(a, profile)

    @t.command("save")
    def save_cmd(
        profile: str = typer.Argument(..., help="Name for the new profile.",
                                      autocompletion=_complete_profiles(a)),
        force: bool = typer.Option(False, "--force", help="Overwrite existing profile."),
    ) -> None:
        """Save the current live config as a profile."""
        _cmd_save(a, profile, force)

    @t.command("delete")
    def delete_cmd(
        profile: str = typer.Argument(..., autocompletion=_complete_profiles(a)),
        force: bool = typer.Option(False, "--force", help="Delete even if in use."),
    ) -> None:
        """Delete a saved profile."""
        _cmd_delete(a, profile, force)

    @t.command("show")
    def show_cmd(
        profile: str = typer.Argument(..., autocompletion=_complete_profiles(a)),
    ) -> None:
        """Print a profile's stored files."""
        _cmd_show(a, profile)

    @t.command("clear")
    def clear_cmd() -> None:
        """Remove provider overrides (back to the official login)."""
        _cmd_clear(a)

    @t.command("official")
    def official_cmd() -> None:
        """Return to the official login/config (same as clear)."""
        _cmd_clear(a)

    @t.command("run")
    def run_cmd(
        profile: str = typer.Argument(..., help="Profile name, or 'official'.",
                                      autocompletion=_complete_profiles(a)),
    ) -> None:
        """Switch to a profile, then launch the app."""
        _cmd_run(a, profile)

    @t.command("edit")
    def edit_cmd(
        profile: str = typer.Argument(..., autocompletion=_complete_profiles(a)),
    ) -> None:
        """Edit a profile's main config in $EDITOR."""
        _cmd_edit(a, profile)

    return t


for _a in APPS_ORDER:
    app.add_typer(_make_subapp(_a), name=_a.name,
                  help=f"Manage {_a.name} provider profiles.")


# ---------------------------------------------------------------- sync commands

def _ask_password(confirm: bool = False) -> str:
    pw = os.environ.get("AIS_SYNC_PASSWORD")
    if pw is not None:
        return pw
    return typer.prompt("Sync password", hide_input=True,
                        confirmation_prompt=confirm)


sync_t = typer.Typer(help="Sync provider profiles via Seafile "
                          "(password-encrypted zip).",
                     no_args_is_help=True)


@sync_t.command("setup")
def sync_setup_cmd(
    url: str = typer.Option("", "--url", help="Seafile server URL "
                          "(e.g. https://seafile.example.com)."),
    token: str = typer.Option("", "--token", help="Seafile API token."),
    repo: str = typer.Option("", "--repo", help="Seafile library name."),
) -> None:
    """First-time setup: read URL/token/password, store token encrypted."""
    home = core.get_home()
    url = url or typer.prompt("Seafile server URL").strip()
    token = token or typer.prompt("Seafile API token", hide_input=True).strip()
    repo = repo or typer.prompt("Seafile library name",
                                default=sync_mod.REPO_NAME_DEFAULT)
    password = _ask_password(confirm=True)
    cfg = sync_mod.setup(home, url, token, repo, password)
    typer.echo(f"sync configured: {cfg['url']} library '{cfg['repo_name']}' "
               f"-> {cfg['remote_path']}")
    typer.echo(f"API token stored encrypted with your password in "
               f"{sync_mod.sync_config_path(home)} (this file is never synced)")


@sync_t.command("push")
def sync_push_cmd(
    force: bool = typer.Option(False, "--force", help="Allow pushing an "
                               "empty profile set."),
) -> None:
    """Compress local profiles (password) and upload to Seafile."""
    home = core.get_home()
    typer.echo(sync_mod.push(home, _ask_password(), force=force))


@sync_t.command("pull")
def sync_pull_cmd(
    yes: bool = typer.Option(False, "--yes", help="Replace local profiles "
                             "without asking."),
) -> None:
    """Download from Seafile, decrypt and replace local profiles."""

    def _confirm(files: dict) -> bool:
        typer.echo(sync_mod._profile_summary(files))
        return typer.confirm("Replace local codex/ and claude/ profiles with "
                             "the downloaded set?", default=False)

    home = core.get_home()
    typer.echo(sync_mod.pull(home, _ask_password(), yes=yes, confirm=_confirm))


@sync_t.command("status")
def sync_status_cmd(
    remote: bool = typer.Option(False, "--remote", help="Also fetch remote "
                                "archive info (needs password)."),
) -> None:
    """Show sync configuration and, optionally, the remote archive."""
    home = core.get_home()
    try:
        cfg = sync_mod.load_sync_config(home)
    except core.AisError as e:
        typer.secho(str(e), fg=typer.colors.RED, err=True)
        raise typer.Exit(1)
    typer.echo(f"server: {cfg['url']}")
    typer.echo(f"library: {cfg['repo_name']} ({cfg['repo_id']})")
    typer.echo(f"remote file: {cfg.get('remote_path', sync_mod.REMOTE_PATH_DEFAULT)}")
    typer.echo("token: stored encrypted with your sync password")
    if remote:
        detail = sync_mod.remote_status(home, _ask_password())
        if detail is None:
            typer.echo("remote archive: not uploaded yet")
        else:
            from datetime import datetime
            mtime = datetime.fromtimestamp(detail.get("mtime", 0))
            typer.echo(f"remote archive: {detail.get('size', '?')} bytes, "
                       f"last modified {mtime:%Y-%m-%d %H:%M:%S}")


@sync_t.command("passwd")
def sync_passwd_cmd() -> None:
    """Change the sync password (re-encrypts the stored token)."""
    home = core.get_home()
    old = _ask_password()
    new = _ask_password(confirm=True)
    sync_mod.change_password(home, old, new)
    typer.echo("sync password changed; token re-encrypted")


app.add_typer(sync_t, name="sync",
              help="Sync profiles via Seafile (password-encrypted zip).")


# ---------------------------------------------------------------- usage command

@app.command("usage")
def usage_cmd(
    app_name: str = typer.Argument("", help="codex or claude (default: both)."),
    days: int = typer.Option(0, "--days", min=0,
                             help="Only count activity from the last N days."),
    currency: str = typer.Option("cny", "--currency", "-c",
                                 help="Cost currency: cny (default) or usd."),
    as_json: bool = typer.Option(False, "--json", help="Machine-readable output."),
) -> None:
    """Estimate token usage and cost from local Codex/Claude Code logs."""
    home = core.get_home()
    apps = [app_name] if app_name else ["codex", "claude"]
    if any(a not in ("codex", "claude") for a in apps):
        _die(f"unknown app {app_name!r}; expected 'codex' or 'claude'")
    if currency.lower() not in ("cny", "usd"):
        _die(f"unsupported currency {currency!r}; expected 'cny' or 'usd'")

    reports = [usage_mod.report(home, a, days, currency) for a in apps]
    if as_json:
        typer.echo(json.dumps(reports, indent=2))
        return

    symbol = "¥" if reports[0]["currency"] == "CNY" else "$"
    rate_note = ""
    if reports[0]["currency"] == "CNY":
        rate_note = f", converted at {reports[0]['usd_to_cny']} CNY/USD"

    for r in reports:
        span = f"last {r['days']} days" if r["days"] else "all time"
        rate = f", 1 USD = {r['usd_to_cny']} CNY" if r["currency"] == "CNY" else ""
        typer.echo(f"{r['app']} — usage by model ({span}{rate}), source: {r['source']}")
        if not r["rows"]:
            typer.echo("  no usage data found\n")
            continue
        header = f"  {'model':<24}{'input':>12}{'cache R':>12}{'cache W':>12}{'output':>12}{'cost':>13}"
        typer.echo(header)
        typer.echo("  " + "-" * (len(header) - 2))
        for row in r["rows"]:
            cost = f"{symbol}{row['cost']:,.2f}" if row["cost"] is not None else "—"
            typer.echo(f"  {row['model'][:24]:<24}{row['input']:>12,}"
                       f"{row['cached_input']:>12,}{row['cache_write']:>12,}"
                       f"{row['output']:>12,}{cost:>13}")
        total = f"{symbol}{r['total_cost']:,.2f}" if r["rows"] else ""
        typer.echo("  " + "-" * (len(header) - 2))
        typer.echo(f"  {'total':<24}"
                   f"{sum(x['input'] for x in r['rows']):>12,}"
                   f"{sum(x['cached_input'] for x in r['rows']):>12,}"
                   f"{sum(x['cache_write'] for x in r['rows']):>12,}"
                   f"{sum(x['output'] for x in r['rows']):>12,}"
                   f"{total:>13}")
        unpriced = r["priced_models"] < len(r["rows"])
        note = f"costs are estimates at public list prices (updated {r['prices_updated']}"
        note += f"{rate_note}, short context); edit ~/.config/ais/prices.json to override"
        if unpriced:
            note = f"models without public prices show '—' ({len(r['rows']) - r['priced_models']} unpriced). " + note
        typer.echo(f"  ({note})\n")


# ---------------------------------------------------------------- generic commands

@app.command("attribution")
def attribution_cmd(
    action: str = typer.Argument("", help="'on' (default) or 'off'; omit to "
                                       "show the current state."),
) -> None:
    """Hide AI attribution (Co-authored-by trailers) in git commits.

    On: `use`/`run` inject codex `[features] commit_attribution_enabled = false`
    and claude `includeCoAuthoredBy = false` + `attribution.commit = ""` into
    the live config. Injected keys are stripped again by `save`; `clear` /
    `official` keep it applied by writing a minimal live config that holds
    only the hide keys (the live config is deleted entirely when off).
    """
    home = core.get_home()
    if action in ("", "show"):
        typer.echo(f"hide AI commit attribution: {'on' if core.hide_ai_attribution(home) else 'off'}")
        return
    if action not in ("on", "off"):
        _die(f"unknown action {action!r}; expected 'on' or 'off'")
    core.set_hide_ai_attribution(home, action == "on")
    typer.echo(f"hide AI commit attribution: {action}")


@app.command("status")
def status_cmd() -> None:
    """Show current profile and official-login status for each app."""
    home = core.get_home()
    for a in APPS_ORDER:
        names = core.list_profiles(home, a)
        typer.echo(f"{a.name}:")
        typer.echo(f"  current: {core.resolve_current(home, a)}")
        typer.echo(f"  official auth: {core.auth_status(home, a)}")
        typer.echo(f"  profiles: {len(names)}"
                   + (f" ({', '.join(names)})" if names else ""))
    typer.echo(f"hide AI commit attribution: {'on' if core.hide_ai_attribution(home) else 'off'}")


@app.command("backup")
def backup_cmd() -> None:
    """Manually back up the current live configs."""
    home = core.get_home()
    for a in APPS_ORDER:
        b = core.backup_live(home, a)
        typer.echo(f"{a.name}: {b if b else 'no live config, skipped'}")


@app.command("validate")
def validate_cmd() -> None:
    """Validate profile files and live configs (syntax only)."""
    home = core.get_home()
    problems = _check_all(home)
    if problems:
        for p in problems:
            typer.secho(p, fg=typer.colors.RED, err=True)
        raise typer.Exit(1)
    n = sum(len(core.list_profiles(home, a)) for a in APPS_ORDER)
    typer.echo(f"valid: {n} profile(s), live configs and state.json")


@app.command("doctor")
def doctor_cmd() -> None:
    """Check the whole ais installation for problems."""
    home = core.get_home()
    for a in APPS_ORDER:
        typer.echo(f"{a.name}: current={core.resolve_current(home, a)}, "
                   f"official auth: {core.auth_status(home, a)}")
    typer.echo(f"ais config dir: {core.ais_dir(home)}")
    problems = _check_all(home)
    if problems:
        for p in problems:
            typer.secho(f"problem: {p}", fg=typer.colors.RED, err=True)
        raise typer.Exit(1)
    typer.echo("all checks passed")


def _version_cb(value: bool):
    if value:
        typer.echo(f"ais {__version__}")
        raise typer.Exit()


@app.callback()
def _root(
    version: bool = typer.Option(False, "--version", callback=_version_cb,
                                 is_eager=True, help="Show version and exit."),
) -> None:
    """ais — switch provider profiles for Codex CLI and Claude Code."""


if __name__ == "__main__":
    app()
