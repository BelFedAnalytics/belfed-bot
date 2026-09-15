"""Offline-проверки модуля микро-опросов: структура волн, клавиатуры, тексты.

Сеть и база не трогаются: тестируется только чистая логика рендера и разбора
callback_data, то есть та часть, где ошибка ломает опрос у живого участника.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("SUPABASE_URL", "https://example.supabase.co")
os.environ.setdefault("SUPABASE_SERVICE_ROLE_KEY", "test-key")

import surveys  # noqa: E402
from telegram import InlineKeyboardButton, InlineKeyboardMarkup  # noqa: E402


def test_every_question_has_unique_codes_in_both_langs():
    for wave, spec in surveys.WAVES.items():
        keys = [q["key"] for q in spec["questions"]]
        assert len(keys) == len(set(keys)), f"{wave}: duplicate question keys"
        for q in spec["questions"]:
            for lang in ("ru", "en"):
                opts = surveys._options_for(q, lang)
                codes = [c for c, _ in opts]
                labels = [l for _, l in opts]
                assert codes, f"{wave}/{q['key']}: no options"
                assert len(codes) == len(set(codes)), f"{wave}/{q['key']}: duplicate codes"
                assert all(labels), f"{wave}/{q['key']}: empty label in {lang}"
                assert q["text"][lang], f"{wave}/{q['key']}: no text in {lang}"


def test_callback_data_round_trip():
    """Каждая кнопка опроса должна разбираться обратно в существующий вопрос."""
    for wave, spec in surveys.WAVES.items():
        for idx, q in enumerate(spec["questions"]):
            kb = surveys._keyboard(wave, q, "en", [])
            datas = [b.callback_data for row in kb.inline_keyboard for b in row]
            assert datas
            for d in datas:
                parts = d.split(":", 3)
                assert len(parts) == 4, d
                assert parts[0] == "sv"
                assert parts[1] == wave
                assert surveys._q_index(wave, parts[2]) == idx
                known = [c for c, _ in surveys._options_for(q, "en")]
                assert parts[3] in known + ["_done"], d


def test_multi_pick_questions_offer_done_button():
    for wave, spec in surveys.WAVES.items():
        for q in spec["questions"]:
            kb = surveys._keyboard(wave, q, "ru", [])
            datas = [b.callback_data for row in kb.inline_keyboard for b in row]
            has_done = any(d.endswith(":_done") for d in datas)
            assert has_done == (q["max_pick"] > 1), f"{wave}/{q['key']}"


def test_scale_questions_include_did_not_use():
    for wave, spec in surveys.WAVES.items():
        for q in spec["questions"]:
            if not q.get("scale"):
                continue
            for lang in ("ru", "en"):
                codes = [c for c, _ in surveys._options_for(q, lang)]
                assert codes == ["1", "2", "3", "4", "5", "skip"], f"{wave}/{q['key']}"


def test_picked_options_are_marked():
    q = surveys.WAVES["fm0"]["questions"][0]
    first_code = q["options"][0][0]
    kb = surveys._keyboard("fm0", q, "en", [first_code])
    marked = [b.text for row in kb.inline_keyboard for b in row
              if b.callback_data == f"sv:fm0:{q['key']}:{first_code}"]
    assert marked and marked[0].startswith("✅")


def test_intro_only_on_first_card_and_progress_is_shown():
    first = surveys._question_text("fm0", 0, "en", first=True)
    second = surveys._question_text("fm0", 1, "en", first=False)
    intro = surveys.WAVES["fm0"]["intro"]["en"]
    assert intro in first
    assert intro not in second
    total = len(surveys.WAVES["fm0"]["questions"])
    assert f"1/{total}" in first
    assert f"2/{total}" in second


def test_multi_pick_hint_mentions_the_limit():
    q1 = surveys.WAVES["fm0"]["questions"][0]
    text_en = surveys._question_text("fm0", 0, "en", first=True)
    text_ru = surveys._question_text("fm0", 0, "ru", first=True)
    assert str(q1["max_pick"]) in text_en
    assert str(q1["max_pick"]) in text_ru


def test_letter_has_no_long_dash_and_no_dropped_quota_claim():
    """Владелец не использует длинные тире, а обещание «3 запроса в день» снято."""
    for lang, body in surveys.LETTER.items():
        assert "\u2014" not in body, f"long dash in {lang} letter"
        assert "3 asset requests" not in body
        assert "3 заявки" not in body
    for lang, body in surveys.REQUEST_BLOCK.items():
        assert "\u2014" not in body, f"long dash in {lang} request block"
    for wave, spec in surveys.WAVES.items():
        for lang in ("ru", "en"):
            assert "\u2014" not in spec["final"][lang], f"long dash in {wave} final/{lang}"


def test_letter_placeholders_render():
    text = surveys.LETTER["en"].format(name="Tom", n=5, request_block="")
    assert "Tom" in text and "number 5" in text
    assert "{" not in text


def test_letter_keyboard_layout():
    kb = surveys.letter_keyboard("fm0", "en", with_request=True)
    datas = [b.callback_data or b.url for row in kb.inline_keyboard for b in row]
    assert "sv:start:fm0" in datas
    assert "feedback_open" in datas
    assert any(d and "start=request" in d for d in datas)

    kb_no_req = surveys.letter_keyboard("fm0", "en", with_request=False)
    datas_no_req = [b.callback_data or b.url for row in kb_no_req.inline_keyboard for b in row]
    assert not any(d and "start=request" in d for d in datas_no_req)


def test_only_the_survey_button_is_stripped_after_start():
    kb = surveys.letter_keyboard("fm0", "ru", with_request=True)
    stripped = surveys.letter_keyboard_without_survey(kb)
    datas = [b.callback_data or b.url for row in stripped.inline_keyboard for b in row]
    assert "sv:start:fm0" not in datas
    assert "feedback_open" in datas
    assert any(d and "start=request" in d for d in datas)


def test_stripping_a_survey_only_keyboard_returns_none():
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("go", callback_data="sv:start:fm0")]])
    assert surveys.letter_keyboard_without_survey(kb) is None
    assert surveys.letter_keyboard_without_survey(None) is None
