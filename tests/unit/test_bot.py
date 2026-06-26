"""Tests for the Telegram operations bot (pure routing + helpers, no network)."""

from __future__ import annotations

import bot

# --- allow-list / authorization ------------------------------------------------


def test_parse_allowlist_handles_commas_spaces_and_garbage() -> None:
    assert bot.parse_allowlist("123, 456 789", "x, 10") == {123, 456, 789, 10}
    assert bot.parse_allowlist(None, "") == set()
    assert bot.parse_allowlist("not-a-number") == set()


def test_is_authorized() -> None:
    allowed = {111, 222}
    assert bot.is_authorized(111, allowed)
    assert not bot.is_authorized(999, allowed)


# --- command parsing -----------------------------------------------------------


def test_command_for_maps_known_commands() -> None:
    assert bot.command_for("/organize") == "organize"
    assert bot.command_for("/dupes some args") == "dupes_sim"
    assert bot.command_for("/analyst@izumi_bot") == "analyst"  # strips @botname
    assert bot.command_for("/nope") is None
    assert bot.command_for("") is None


# --- routing (decide) ----------------------------------------------------------


def test_decide_denies_unauthorized() -> None:
    assert bot.decide(message_text="/health", authorized=False).kind == "deny"
    assert bot.decide(callback_data="act:health", authorized=False).kind == "deny"


def test_decide_help_and_status() -> None:
    assert bot.decide(message_text="/start", authorized=True).kind == "help"
    assert bot.decide(message_text="/menu", authorized=True).kind == "help"
    assert bot.decide(message_text="/status", authorized=True).kind == "status"


def test_decide_runs_read_only_actions_immediately() -> None:
    d = bot.decide(message_text="/analyst", authorized=True)
    assert d.kind == "run" and d.action == "analyst"


def test_decide_confirms_destructive_actions() -> None:
    d = bot.decide(message_text="/organize", authorized=True)
    assert d.kind == "confirm" and d.action == "organize"


def test_decide_callback_act_destructive_needs_confirm() -> None:
    assert bot.decide(callback_data="act:dupes_real", authorized=True).kind == "confirm"
    assert bot.decide(callback_data="act:dupes_sim", authorized=True).kind == "run"


def test_decide_callback_confirm_runs() -> None:
    d = bot.decide(callback_data="confirm:organize", authorized=True)
    assert d.kind == "run" and d.action == "organize"


def test_decide_cancel_and_unknown() -> None:
    assert bot.decide(callback_data="cancel", authorized=True).kind == "cancel"
    assert bot.decide(callback_data="act:bogus", authorized=True).kind == "unknown"
    assert bot.decide(message_text="hello there", authorized=True).kind == "unknown"


# --- apply routing -------------------------------------------------------------


def test_decide_apply_lists() -> None:
    assert bot.decide(message_text="/apply", authorized=True).kind == "apply_list"


def test_decide_apply_callbacks() -> None:
    confirm = bot.decide(callback_data="apply:2", authorized=True)
    assert confirm.kind == "apply_confirm" and confirm.action == "2"
    run = bot.decide(callback_data="doapply:2", authorized=True)
    assert run.kind == "apply_run" and run.action == "2"


def test_action_at_is_bounds_and_parse_safe() -> None:
    actions = ["a", "b", "c"]
    assert bot._action_at(actions, "1") == "b"
    assert bot._action_at(actions, "9") is None
    assert bot._action_at(actions, "x") is None
    assert bot._action_at([], "0") is None


# --- keyboards -----------------------------------------------------------------


def test_main_keyboard_has_a_button_per_action() -> None:
    kb = bot.main_keyboard()
    buttons = [b for row in kb["inline_keyboard"] for b in row]
    assert len(buttons) == len(bot.ACTIONS)
    assert all(b["callback_data"].startswith("act:") for b in buttons)
    # destructive actions are flagged with a warning glyph
    organize = next(b for b in buttons if b["callback_data"] == "act:organize")
    assert organize["text"].startswith("⚠️")


def test_apply_list_keyboard_has_a_button_per_action() -> None:
    from aictx.apply import ApplyAction

    actions = [
        ApplyAction("docker restart radarr", "docker-lifecycle", "t", "f", "error"),
        ApplyAction("chmod 600 /a", "chmod", "t2", "g", "warning"),
    ]
    kb = bot.apply_list_keyboard(actions)
    buttons = [b for row in kb["inline_keyboard"] for b in row]
    assert [b["callback_data"] for b in buttons] == ["apply:0", "apply:1"]


def test_apply_confirm_keyboard_offers_apply_and_cancel() -> None:
    kb = bot.apply_confirm_keyboard("3")
    flat = {b["callback_data"] for row in kb["inline_keyboard"] for b in row}
    assert flat == {"doapply:3", "cancel"}


def test_confirm_keyboard_offers_confirm_and_cancel() -> None:
    kb = bot.confirm_keyboard("extract")
    flat = {b["callback_data"] for row in kb["inline_keyboard"] for b in row}
    assert flat == {"confirm:extract", "cancel"}


# --- message splitting / formatting --------------------------------------------


def test_split_message_keeps_short_text_in_one_chunk() -> None:
    assert bot.split_message("hola") == ["hola"]
    assert bot.split_message("") == [""]


def test_split_message_is_lossless_and_respects_limit() -> None:
    text = "".join(f"line number {i}\n" for i in range(500))
    chunks = bot.split_message(text, limit=100)
    assert "".join(chunks) == text  # lossless
    assert all(len(c) <= 100 for c in chunks)
    assert len(chunks) > 1


def test_split_message_hard_splits_an_overlong_line() -> None:
    chunks = bot.split_message("x" * 250, limit=100)
    assert [len(c) for c in chunks] == [100, 100, 50]


def test_format_result_header_reflects_returncode() -> None:
    assert bot.format_result("Tarea", 0, "ok").startswith("✅")
    assert bot.format_result("Tarea", 1, "boom").startswith("❌")


def test_format_result_caps_long_body_keeping_the_tail() -> None:
    body = "START" + ("a" * 9000) + "END-OF-REPORT"
    out = bot.format_result("Tarea", 0, body, cap=200)
    assert "recortado" in out
    assert out.endswith("END-OF-REPORT")
    assert "START" not in out  # head dropped, tail kept
