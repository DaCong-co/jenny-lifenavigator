"""life_review: the prompt template defaults to silence and never writes."""

from pathlib import Path


def _read_template(name: str) -> str:
    root = Path(__file__).parents[2]
    return (root / "jenny" / "templates" / "agent" / name).read_text(encoding="utf-8")


class TestLifeReviewTemplate:
    def test_default_silence(self):
        assert "Default to SILENCE" in _read_template("life_review.md")

    def test_three_escalation_thresholds(self):
        text = _read_template("life_review.md")
        assert "Sustained drift" in text
        assert "high-value event" in text
        assert "explicitly asked" in text

    def test_writes_nothing(self):
        assert "You write nothing" in _read_template("life_review.md")

    def test_read_only_on_the_model(self):
        text = _read_template("life_review.md")
        assert "update current_state" in text
        assert "do not record events" in text
