"""ais command-line interface."""

from __future__ import annotations

import json
import os
import shutil
import subprocess

import typer

from . import __version__, core
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
               [("official", "restore baseline / official login")]
    return complete


# ---------------------------------------------------------------- command logic

def _cmd_use(a: App, profile: str) -> None:
    home = core.get_home()
    if profile == "official":
        _cmd_clear(a, hard=False)
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
    try:
        a.validate(content)
    except Exception as e:
        _die(f"generated config for '{profile}' is invalid, refusing to switch: {e}")

    core.ensure_baseline(home, a)
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

    pdir.mkdir(parents=True, exist_ok=True)
    core.atomic_write(pdir / a.live_file, text)
    for w in a.post_save(a, home, pdir):
        _warn(w)
    typer.echo(f"{a.name}: saved live config as profile '{profile}' ({pdir})")
    typer.echo(f"official login files are never copied: {_auth_note(a)}")


def _cmd_clear(a: App, hard: bool) -> None:
    home = core.get_home()
    live = core.live_path(home, a)
    core.ensure_baseline(home, a)
    backup = core.backup_live(home, a)

    if hard:
        if live.exists():
            live.unlink()
            typer.echo(f"{a.name}: removed {live} (--hard)")
        else:
            typer.echo(f"{a.name}: no live config at {live}")
        core.set_state(home, a, current=None, live_hash=None, official=True)
    else:
        try:
            existed, content = core.read_baseline(home, a)
        except (OSError, ValueError) as e:
            _die(f"cannot read {a.name} baseline: {e}; "
                 f"try 'ais {a.name} reset-baseline'")
        if existed and content is not None:
            try:
                a.validate(content)
            except Exception as e:
                _die(f"baseline config is invalid, live config left untouched: {e}")
            core.atomic_write(live, content)
            core.set_state(home, a, current=None,
                           live_hash=core.sha256_text(content), official=True)
            typer.echo(f"{a.name}: restored pre-ais baseline config")
        else:
            if live.exists():
                live.unlink()
            core.set_state(home, a, current=None, live_hash=None, official=True)
            typer.echo(f"{a.name}: baseline had no {a.live_file}; removed live config")

    if backup:
        typer.echo(f"backup: {backup}")
    typer.echo(f"official login untouched: {_auth_note(a)}")


def _cmd_run(a: App, profile: str) -> None:
    home = core.get_home()
    if profile == "official":
        _cmd_clear(a, hard=False)
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

    for a in APPS_ORDER:
        live = core.live_path(home, a)
        if live.is_file():
            try:
                a.validate(live.read_text(encoding="utf-8"))
            except Exception as e:
                problems.append(f"live {a.name} config ({live}): {e}")

        if core.baseline_captured(home, a):
            try:
                existed, content = core.read_baseline(home, a)
                if existed and content is not None:
                    a.validate(content)
            except Exception as e:
                problems.append(f"{a.name} baseline: {e}")

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
    def clear_cmd(
        hard: bool = typer.Option(False, "--hard",
                                  help="Delete the live config instead of restoring "
                                       "the baseline."),
    ) -> None:
        """Remove provider overrides (restore the pre-ais baseline)."""
        _cmd_clear(a, hard)

    @t.command("official")
    def official_cmd() -> None:
        """Return to the official login/config (same as clear)."""
        _cmd_clear(a, hard=False)

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

    @t.command("reset-baseline")
    def reset_baseline_cmd() -> None:
        """Re-capture the baseline from the current live config."""
        core.reset_baseline(core.get_home(), a)
        typer.echo(f"{a.name}: baseline reset from current live config")

    return t


for _a in APPS_ORDER:
    app.add_typer(_make_subapp(_a), name=_a.name,
                  help=f"Manage {_a.name} provider profiles.")


# ---------------------------------------------------------------- generic commands

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
        typer.echo(f"  baseline: "
                   f"{'captured' if core.baseline_captured(home, a) else 'not captured'}")


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
