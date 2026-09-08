"""Dream Life Model pass: the anti-labeling rules are present in the template."""

from pathlib import Path


def _read_template(name: str) -> str:
    root = Path(__file__).parents[2]
    return (root / "jenny" / "templates" / "agent" / name).read_text(encoding="utf-8")


class TestDreamLifeModelTemplate:
    def test_facts_not_inferences(self):
        assert "Facts, not inferences" in _read_template("life_model_update.md")

    def test_evidence_threshold_two_independent(self):
        assert "at least 2 independent pieces of evidence" in _read_template("life_model_update.md")

    def test_observed_period_required(self):
        assert "observed_period" in _read_template("life_model_update.md")

    def test_candidate_is_never_a_confirmed_pattern(self):
        assert "never a confirmed pattern" in _read_template("life_model_update.md")

    def test_user_statements_win_over_inference(self):
        text = _read_template("life_model_update.md")
        assert "USER.md" in text
        assert "life_goals win" in text

    def test_never_talks_to_user(self):
        assert "Never talk to the user" in _read_template("life_model_update.md")
