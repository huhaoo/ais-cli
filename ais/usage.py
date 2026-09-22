"""Usage statistics from local session logs, priced at public list rates.

ais never sees API traffic, so usage is reconstructed from the logs the two
CLIs already write on disk:

- Claude Code: ~/.claude/projects/*/*.jsonl — ``assistant`` entries carry
  ``message.usage``; streaming updates rewrite the same message id, so the
  last occurrence per id is kept (dedup), and resumed sessions repeat ids
  across files — dedup is global.
- Codex: ~/.codex/sessions/**/rollout-*.jsonl — per-request
  ``token_usage_record`` entries (model resolved via ``turn_context``
  by turn_id); older releases only emit cumulative ``token_count`` events,
  in which case the last cumulative total is used once per session file.

Cost is an *estimate* at public list prices (ais/prices.json, overridable
via ~/.config/ais/prices.json). Third-party providers may charge differently.
"""

from __future__ import annotations

import json
import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

BUNDLED_PRICES = Path(__file__).with_name("prices.json")


@dataclass
class Tokens:
    input: int = 0
    cached_input: int = 0
    cache_write: int = 0
    output: int = 0

    def add(self, inp=0, cached=0, write=0, out=0) -> None:
        self.input += inp or 0
        self.cached_input += cached or 0
        self.cache_write += write or 0
        self.output += out or 0

    def __add__(self, other: "Tokens") -> "Tokens":
        return Tokens(self.input + other.input,
                      self.cached_input + other.cached_input,
                      self.cache_write + other.cache_write,
                      self.output + other.output)

    @property
    def total(self) -> int:
        return self.input + self.cached_input + self.cache_write + self.output


# ---------------------------------------------------------------- prices

def load_prices(home: Path) -> dict:
    """Bundled price table with user overrides from ~/.config/ais/prices.json."""
    data = json.loads(BUNDLED_PRICES.read_text(encoding="utf-8"))
    data.setdefault("per_mtok", {})
    user_file = home / ".config" / "ais" / "prices.json"
    if user_file.is_file():
        try:
            user = json.loads(user_file.read_text(encoding="utf-8"))
            data["per_mtok"].update(user.get("per_mtok", {}))
            if isinstance(user.get("usd_to_cny"), (int, float)):
                data["usd_to_cny"] = user["usd_to_cny"]
            data["user_override"] = str(user_file)
        except (OSError, ValueError):
            data["user_override_error"] = str(user_file)
    return data


def normalize_model(name: str) -> str:
    return re.sub(r"\[.*\]$", "", (name or "unknown").strip()).lower() or "unknown"


def match_price(model: str, prices: dict) -> Optional[dict]:
    """Exact key first, then longest key that is a prefix at a '-' or '.' boundary."""
    table = prices.get("per_mtok", {})
    if model in table:
        return table[model]
    candidates = [k for k in table
                  if model.startswith(k + "-") or model.startswith(k + ".")]
    return table[max(candidates, key=len)] if candidates else None


def native_currency(price: dict) -> str:
    return str(price.get("currency", "usd")).upper()


def cost_of(tokens: Tokens, price: dict) -> float:
    """Cost of a token bundle, in the price entry's native currency, per 1M rates."""
    inp = price["input"]
    cached = price.get("cached_input", inp * 0.1)
    # cache-write premium is Anthropic-specific (1.25x, set explicitly there);
    # everyone else bills cache writes as ordinary input
    write = price.get("cache_write", inp)
    return (tokens.input * inp + tokens.cached_input * cached
            + tokens.cache_write * write + tokens.output * price["output"]) / 1e6


def to_currency(cost: float, frm: str, to: str, prices: dict) -> float:
    """Convert between native price currencies; only USD<->CNY needed."""
    frm, to = frm.upper(), to.upper()
    if frm == to:
        return cost
    rate = float(prices.get("usd_to_cny") or 6.71)
    return cost * rate if to == "CNY" else cost / rate


# ---------------------------------------------------------------- helpers

def _parse_ts(value) -> Optional[datetime]:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _iter_jsonl(path: Path):
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except ValueError:
                    continue
    except OSError:
        return


def _cutoff(days: int) -> Optional[datetime]:
    if days <= 0:
        return None
    return datetime.now().astimezone() - timedelta(days=days)


def _usable(path: Path, cutoff: Optional[datetime]) -> bool:
    if cutoff is None:
        return True
    try:
        return datetime.fromtimestamp(path.stat().st_mtime).astimezone() >= cutoff
    except OSError:
        return False


# ---------------------------------------------------------------- parsers

def parse_claude(home: Path, cutoff: Optional[datetime]) -> "tuple[dict[str, Tokens], int]":
    """~/.claude/projects/*/*.jsonl -> {model: Tokens}; dedup by message id."""
    per_model: "dict[str, Tokens]" = defaultdict(Tokens)
    files = 0
    proj = home / ".claude" / "projects"
    # last occurrence per message id wins; ids repeat across files on resume
    latest: "dict[object, tuple]" = {}
    if proj.is_dir():
        for f in sorted(proj.glob("*/*.jsonl")):
            if not _usable(f, cutoff):
                continue
            files += 1
            for lineno, d in enumerate(_iter_jsonl(f)):
                if d.get("type") != "assistant":
                    continue
                msg = d.get("message") or {}
                u = msg.get("usage") or {}
                if not u:
                    continue
                key = msg.get("id") or (f.name, lineno)
                ts = _parse_ts(d.get("timestamp"))
                latest[key] = (normalize_model(msg.get("model")),
                               u.get("input_tokens") or 0,
                               u.get("cache_read_input_tokens") or 0,
                               u.get("cache_creation_input_tokens") or 0,
                               u.get("output_tokens") or 0,
                               ts)
    for model, inp, cached, write, out, ts in latest.values():
        if cutoff is not None and (ts is None or ts < cutoff):
            continue
        per_model[model].add(inp, cached, write, out)
    return dict(per_model), files


def parse_codex(home: Path, cutoff: Optional[datetime]) -> "tuple[dict[str, Tokens], int]":
    """~/.codex/sessions/**/rollout-*.jsonl -> {model: Tokens}.

    OpenAI usage semantics: ``input_tokens`` is the FULL prompt size and
    ``cached_input_tokens`` / ``cache_write_input_tokens`` are subsets of it,
    so the uncached input billed at full rate is input - cached - write.
    (Anthropic semantics, used by parse_claude, already reports uncached
    input in ``input_tokens``.)
    """
    def buckets(u: dict) -> tuple:
        cached = u.get("cached_input_tokens") or 0
        write = u.get("cache_write_input_tokens") or 0
        fresh = max((u.get("input_tokens") or 0) - cached - write, 0)
        return fresh, cached, write, u.get("output_tokens")

    per_model: "dict[str, Tokens]" = defaultdict(Tokens)
    files = 0
    seen_responses: set = set()
    sdir = home / ".codex" / "sessions"
    if sdir.is_dir():
        for f in sorted(sdir.rglob("*.jsonl")):
            if not _usable(f, cutoff):
                continue
            files += 1
            turn_models: dict = {}
            records: "list[tuple]" = []
            last_model = "unknown"
            last_total = None
            last_total_ts = None
            for d in _iter_jsonl(f):
                t = d.get("type")
                payload = d.get("payload") or {}
                if t == "turn_context":
                    model = payload.get("model")
                    if model:
                        model = normalize_model(model)
                        last_model = model
                        if payload.get("turn_id"):
                            turn_models[payload["turn_id"]] = model
                elif t == "token_usage_record":
                    rid = payload.get("response_id")
                    if rid:
                        if rid in seen_responses:
                            continue
                        seen_responses.add(rid)
                    records.append((payload.get("turn_id"), payload.get("usage") or {},
                                    _parse_ts(d.get("timestamp"))))
                elif t == "event_msg" and payload.get("type") == "token_count":
                    total = (payload.get("info") or {}).get("total_token_usage") or {}
                    if total:
                        last_total = total
                        last_total_ts = _parse_ts(d.get("timestamp"))
            if records:
                # authoritative per-request records
                for turn_id, u, ts in records:
                    if cutoff is not None and (ts is None or ts < cutoff):
                        continue
                    model = turn_models.get(turn_id) or last_model
                    per_model[model].add(*buckets(u))
            elif last_total:
                # older codex: one cumulative total per session file
                if cutoff is not None and (last_total_ts is None or last_total_ts < cutoff):
                    continue
                per_model[last_model].add(*buckets(last_total))
    return dict(per_model), files


# ---------------------------------------------------------------- report

def report(home: Path, app: str, days: int = 0, currency: str = "cny") -> dict:
    """Aggregate one app. app is 'codex' or 'claude'; currency 'cny' or 'usd'."""
    cutoff = _cutoff(days)
    if app == "claude":
        per_model, files = parse_claude(home, cutoff)
        source = "~/.claude/projects"
    else:
        per_model, files = parse_codex(home, cutoff)
        source = "~/.codex/sessions"

    currency = currency.upper()
    if currency not in ("CNY", "USD"):
        raise ValueError(f"unsupported currency {currency!r}")
    prices = load_prices(home)
    rows = []
    for model in sorted(per_model, key=lambda m: -per_model[m].total):
        if per_model[model].total == 0:  # e.g. claude "<synthetic>" entries
            continue
        tokens = per_model[model]
        price = match_price(model, prices)
        cost = (to_currency(cost_of(tokens, price), native_currency(price),
                            currency, prices)
                if price else None)
        rows.append({
            "model": model,
            "input": tokens.input,
            "cached_input": tokens.cached_input,
            "cache_write": tokens.cache_write,
            "output": tokens.output,
            "cost": cost,
        })
    return {
        "app": app,
        "source": source,
        "files_scanned": files,
        "days": days,
        "currency": currency,
        "usd_to_cny": prices.get("usd_to_cny"),
        "rows": rows,
        "total_cost": sum(r["cost"] for r in rows if r["cost"] is not None),
        "priced_models": sum(1 for r in rows if r["cost"] is not None),
        "prices_updated": prices.get("updated"),
    }
