"""Expression layer: wording only, and nothing that leaks machinery."""

from __future__ import annotations

from jenny.navigator.expression import ExpressionLayer, MAX_MESSAGE_CHARS


def test_sanitize_strips_fences_quotes_and_whitespace():
    assert ExpressionLayer.sanitize('```\n“最近睡得少，要不要早点休息？”\n```') == (
        "最近睡得少，要不要早点休息？"
    )


def test_sanitize_drops_model_boilerplate():
    assert ExpressionLayer.sanitize(
        "As an AI, I think you should sleep more."
    ) is None


def test_sanitize_caps_length_at_a_sentence_boundary():
    text = ("最近状态有些变化。" * 100)
    cleaned = ExpressionLayer.sanitize(text)
    assert cleaned is not None
    assert len(cleaned) <= MAX_MESSAGE_CHARS
    assert cleaned.endswith("。")


def test_sanitize_keeps_empty_output_empty():
    assert ExpressionLayer.sanitize("   \n ") is None
