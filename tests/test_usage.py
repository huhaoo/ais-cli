"""Tests for usage statistics (synthetic session logs in a sandbox home)."""

import json
from datetime import datetime, timedelta, timezone

import pytest
from typer.testing import CliRunner

from ais import usage
from ais.cli import app as cli_app

runner = CliRunner()
NOW = datetime.now(timezone.utc).isoformat()
OLD = (datetime.now(timezone.utc) - timedelta(days=90)).isoformat()


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("AIS_HOME", str(tmp_path))
    return tmp_path


# ------------------------------------------------------------------ prices

def test_price_matching(home):
    prices = usage.load_prices(home)
    assert usage.match_price("gpt-6-astra", prices)["input"] == 10.00
    # snapshot suffix resolved by boundary prefix
    p = usage.match_price("claude-sonnet-5-20260101", prices)
    assert p["input"] == 2.00 and p["output"] == 10.00
    # most specific key wins
    assert usage.match_price("gpt-5.1-codex-max", prices)["input"] == 1.25
    # boundary prevents gpt-5 matching gpt-5.6-sol
    assert usage.match_price("gpt-5.6-sol", prices)["input"] == 4.00
    # unknown -> no price
    assert usage.match_price("custom-llm-x", prices) is None


def test_user_price_override(home):
    override = home / ".config" / "ais" / "prices.json"
    override.parent.mkdir(parents=True)
    override.write_text(json.dumps(
        {"per_mtok": {"glm-5.3": {"input": 0.5, "output": 2.0}}}))
    prices = usage.load_prices(home)
    assert usage.match_price("glm-5.3", prices)["output"] == 2.0
    assert usage.match_price("gpt-6-astra", prices)["input"] == 10.00


def test_cost_math(home):
    prices = usage.load_prices(home)
    tokens = usage.Tokens(input=1_000_000, output=1_000_000)
    assert usage.cost_of(tokens, usage.match_price("gpt-6-astra", prices)) == 60.00
    # cache read 10%, cache write has no premium when not explicit (non-Anthropic)
    p = usage.match_price("gpt-5.2", prices)
    t = usage.Tokens(cached_input=1_000_000, cache_write=1_000_000)
    assert usage.cost_of(t, p) == pytest.approx(0.175 + 1.75)
    # Anthropic keeps its explicit 1.25x write premium
    p = usage.match_price("claude-sonnet-5", prices)
    t = usage.Tokens(cache_write=1_000_000)
    assert usage.cost_of(t, p) == pytest.approx(2.50)


def test_native_cny_prices(home):
    prices = usage.load_prices(home)
    # mainland CNY list used as-is when displaying CNY
    glm = usage.match_price("glm-5.3", prices)
    assert glm["currency"] == "cny"
    t = usage.Tokens(input=1_000_000, cached_input=1_000_000, output=1_000_000)
    assert usage.cost_of(t, glm) == 8.00 + 2.00 + 28.00  # CNY, no conversion
    rate = prices["usd_to_cny"]
    assert usage.to_currency(38.0, "CNY", "USD", prices) == pytest.approx(38.0 / rate)
    assert usage.to_currency(60.0, "USD", "CNY", prices) == pytest.approx(60.0 * rate)
    assert usage.to_currency(5.0, "USD", "USD", prices) == 5.0


def test_new_provider_matching(home):
    prices = usage.load_prices(home)
    assert usage.match_price("glm-5.3", prices)["input"] == 8.00
    assert usage.match_price("glm-5.2-air", prices) is None or \
        usage.match_price("glm-5.2-air", prices)["input"] == 8.00  # prefix glm-5.2
    assert usage.match_price("kimi-k2-0905", prices)["input"] == 0.60  # prefix kimi-k2
    assert usage.match_price("kimi-k2.6", prices)["currency"] == "cny"
    # DeepSeek entries are off-peak rates (peak = 2x)
    assert usage.match_price("deepseek-flash", prices)["input"] == 0.15
    assert usage.match_price("deepseek-v4-pro", prices)["output"] == 1.98


# ------------------------------------------------------------------ claude parser

def claude_line(msg_id, model, inp, cache_r, cache_w, out, ts=NOW, typ="assistant"):
    return json.dumps({"type": typ, "timestamp": ts,
                       "message": {"id": msg_id, "model": model,
                                   "usage": {"input_tokens": inp,
                                             "cache_read_input_tokens": cache_r,
                                             "cache_creation_input_tokens": cache_w,
                                             "output_tokens": out}}})


def test_claude_dedup_and_models(home):
    d = home / ".claude" / "projects" / "proj"
    d.mkdir(parents=True)
    lines = [
        # same message id twice (streaming rewrite): last occurrence wins
        claude_line("msg_1", "claude-sonnet-5", 100, 0, 0, 10),
        claude_line("msg_1", "claude-sonnet-5", 100, 0, 0, 50),
        claude_line("msg_2", "claude-sonnet-5", 200, 300, 400, 20),
        claude_line("msg_3", "claude-opus-5", 1000, 0, 0, 500),
        json.dumps({"type": "assistant", "timestamp": NOW,
                    "message": {"id": "msg_4", "model": "m", }}),  # no usage
        json.dumps({"type": "user", "timestamp": NOW}),             # ignored
        "{ not json",                                               # skipped
    ]
    (d / "s1.jsonl").write_text("\n".join(lines) + "\n")
    # resumed session repeats msg_2 in a different file: counted once
    (d / "s2.jsonl").write_text(claude_line("msg_2", "claude-sonnet-5", 200, 300, 400, 20) + "\n")

    per_model, files = usage.parse_claude(home, None)
    assert files == 2
    sonnet = per_model["claude-sonnet-5"]
    assert (sonnet.input, sonnet.cached_input, sonnet.cache_write, sonnet.output) \
        == (300, 300, 400, 70)  # 100+200, dedup'd msg_1=100/50
    assert per_model["claude-opus-5"].input == 1000


def test_claude_day_filter(home):
    d = home / ".claude" / "projects" / "p"
    d.mkdir(parents=True)
    (d / "s.jsonl").write_text(
        claude_line("old", "claude-sonnet-5", 1, 0, 0, 1, ts=OLD) + "\n" +
        claude_line("new", "claude-sonnet-5", 2, 0, 0, 2, ts=NOW) + "\n")
    cutoff = usage._cutoff(30)
    per_model, _ = usage.parse_claude(home, cutoff)
    assert per_model["claude-sonnet-5"].input == 2


# ------------------------------------------------------------------ codex parser

def test_codex_token_usage_records(home):
    sdir = home / ".codex" / "sessions" / "2026" / "09" / "14"
    sdir.mkdir(parents=True)
    lines = [
        json.dumps({"timestamp": NOW, "type": "turn_context",
                    "payload": {"turn_id": "t1", "model": "gpt-6-astra"}}),
        json.dumps({"timestamp": NOW, "type": "token_usage_record",
                    "payload": {"turn_id": "t1", "response_id": "r1",
                                "usage": {"input_tokens": 8000, "cached_input_tokens": 5000,
                                          "cache_write_input_tokens": 2000, "output_tokens": 300}}}),
        json.dumps({"timestamp": NOW, "type": "turn_context",
                    "payload": {"turn_id": "t2", "model": "glm-5.3"}}),
        json.dumps({"timestamp": NOW, "type": "token_usage_record",
                    "payload": {"turn_id": "t2", "response_id": "r2",
                                "usage": {"input_tokens": 10, "cached_input_tokens": 0,
                                          "cache_write_input_tokens": 0, "output_tokens": 5}}}),
        # duplicate response_id: ignored
        json.dumps({"timestamp": NOW, "type": "token_usage_record",
                    "payload": {"turn_id": "t2", "response_id": "r2",
                                "usage": {"input_tokens": 999, "output_tokens": 999}}}),
    ]
    (sdir / "rollout-x.jsonl").write_text("\n".join(lines) + "\n")
    per_model, files = usage.parse_codex(home, None)
    assert files == 1
    # OpenAI semantics: input_tokens includes cached+write -> fresh = 8000-5000-2000
    assert per_model["gpt-6-astra"].input == 1000
    assert per_model["gpt-6-astra"].cached_input == 5000
    assert per_model["gpt-6-astra"].cache_write == 2000
    assert per_model["glm-5.3"].input == 10
    assert per_model["glm-5.3"].output == 5


def test_codex_legacy_cumulative_token_count(home):
    sdir = home / ".codex" / "sessions" / "2026" / "01" / "01"
    sdir.mkdir(parents=True)
    total_v1 = {"input_tokens": 100, "cached_input_tokens": 10,
                "cache_write_input_tokens": 20, "output_tokens": 30}
    total_v2 = dict(total_v1, input_tokens=500)  # cumulative: last wins
    lines = [
        json.dumps({"timestamp": NOW, "type": "turn_context",
                    "payload": {"turn_id": "t1", "model": "gpt-5.2"}}),
        json.dumps({"timestamp": NOW, "type": "event_msg",
                    "payload": {"type": "token_count",
                                "info": {"total_token_usage": total_v1}}}),
        json.dumps({"timestamp": NOW, "type": "event_msg",
                    "payload": {"type": "token_count",
                                "info": {"total_token_usage": total_v2}}}),
    ]
    (sdir / "rollout-old.jsonl").write_text("\n".join(lines) + "\n")
    per_model, _ = usage.parse_codex(home, None)
    assert per_model["gpt-5.2"].input == 470  # 500 total - 10 cached - 20 write
    assert per_model["gpt-5.2"].output == 30


def test_codex_cost_no_double_count(home):
    """Codex input_tokens includes cached/write subsets; they must not be
    billed twice (regression: input was billed at full rate AND as cache)."""
    sdir = home / ".codex" / "sessions" / "2026" / "09" / "14"
    sdir.mkdir(parents=True)
    lines = [
        json.dumps({"timestamp": NOW, "type": "turn_context",
                    "payload": {"turn_id": "t1", "model": "gpt-5.2"}}),
        json.dumps({"timestamp": NOW, "type": "token_usage_record",
                    "payload": {"turn_id": "t1", "response_id": "r1",
                                "usage": {"input_tokens": 1_000_000,
                                          "cached_input_tokens": 900_000,
                                          "cache_write_input_tokens": 0,
                                          "output_tokens": 0}}}),
    ]
    (sdir / "rollout.jsonl").write_text("\n".join(lines) + "\n")
    r = usage.report(home, "codex", currency="usd")
    # fresh 100k x $1.75 + cached 900k x $0.175 = 0.175 + 0.1575
    assert r["rows"][0]["cost"] == pytest.approx(0.3325)
    assert r["rows"][0]["input"] == 100_000


# ------------------------------------------------------------------ report + CLI

def test_report_costs(home):
    d = home / ".claude" / "projects" / "p"
    d.mkdir(parents=True)
    (d / "s.jsonl").write_text(
        claude_line("m1", "claude-sonnet-5", 1_000_000, 0, 0, 1_000_000) + "\n" +
        claude_line("m2", "custom-llm-x", 500_000, 0, 0, 500_000) + "\n")
    r = usage.report(home, "claude")
    by_model = {row["model"]: row for row in r["rows"]}
    rate = r["usd_to_cny"]
    assert by_model["claude-sonnet-5"]["cost"] == pytest.approx(12.00 * rate)  # CNY default
    assert by_model["custom-llm-x"]["cost"] is None
    assert r["total_cost"] == pytest.approx(12.00 * rate)
    assert r["currency"] == "CNY"
    assert r["priced_models"] == 1

    r_usd = usage.report(home, "claude", currency="usd")
    assert r_usd["total_cost"] == pytest.approx(12.00)

    with pytest.raises(ValueError):
        usage.report(home, "claude", currency="eur")


def test_user_exchange_rate_override(home):
    d = home / ".claude" / "projects" / "p"
    d.mkdir(parents=True)
    (d / "s.jsonl").write_text(
        claude_line("m1", "claude-sonnet-5", 1_000_000, 0, 0, 1_000_000) + "\n")
    override = home / ".config" / "ais" / "prices.json"
    override.parent.mkdir(parents=True, exist_ok=True)
    override.write_text(json.dumps({"usd_to_cny": 7.5}))
    r = usage.report(home, "claude")  # CNY default
    assert r["total_cost"] == pytest.approx(12.00 * 7.5)


def test_cli_usage(home):
    d = home / ".claude" / "projects" / "p"
    d.mkdir(parents=True)
    (d / "s.jsonl").write_text(
        claude_line("m1", "claude-sonnet-5", 1_000_000, 0, 0, 1_000_000) + "\n")
    d2 = home / ".codex" / "sessions" / "2026" / "09" / "14"
    d2.mkdir(parents=True)
    (d2 / "rollout.jsonl").write_text(json.dumps({
        "timestamp": NOW, "type": "turn_context",
        "payload": {"turn_id": "t1", "model": "gpt-6-astra"}}) + "\n" + json.dumps({
        "timestamp": NOW, "type": "token_usage_record",
        "payload": {"turn_id": "t1", "response_id": "r1",
                    "usage": {"input_tokens": 1_000_000, "output_tokens": 1_000_000}}}) + "\n")

    r = runner.invoke(cli_app, ["usage"])
    assert r.exit_code == 0, r.output
    rate = usage.load_prices(home)["usd_to_cny"]
    assert "claude-sonnet-5" in r.output and f"¥{12.00 * rate:,.2f}" in r.output
    assert "gpt-6-astra" in r.output and f"¥{60.00 * rate:,.2f}" in r.output
    assert f"converted at {rate} CNY/USD" in r.output

    r = runner.invoke(cli_app, ["usage", "--currency", "usd"])
    assert r.exit_code == 0
    assert "$12.00" in r.output and "$60.00" in r.output
    assert "converted at" not in r.output

    r = runner.invoke(cli_app, ["usage", "codex", "--json"])
    assert r.exit_code == 0
    data = json.loads(r.output)
    assert data[0]["rows"][0]["model"] == "gpt-6-astra"
    assert data[0]["currency"] == "CNY"

    r = runner.invoke(cli_app, ["usage", "bogus"])
    assert r.exit_code == 1
    r = runner.invoke(cli_app, ["usage", "--currency", "eur"])
    assert r.exit_code == 1


def test_cli_usage_empty(home):
    r = runner.invoke(cli_app, ["usage"])
    assert r.exit_code == 0
    assert "no usage data found" in r.output
