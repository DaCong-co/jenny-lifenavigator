"""Injection selectivity: the 4 files are exposed, events/candidates are not."""

import pytest

from jenny.life_model.store import LifeModelStore


@pytest.fixture
def store(tmp_path):
    return LifeModelStore(tmp_path)


class TestLifeModelInjection:
    def test_read_context_files_excludes_events_and_candidates(self, store):
        store.write_event("an event", kind="other")
        store.add_pattern_candidate("a candidate", ["e1", "e2"])
        context = store.read_context_files()
        assert set(context) == {"life_goals", "current_state", "patterns", "expression"}
        assert "events" not in context
        assert "pattern_candidates" not in context

    def test_nonempty_files_are_returned(self, store):
        store.update_current_state("状态: 备考")
        store.life_goals_file.write_text("- 专升本", encoding="utf-8")
        context = store.read_context_files()
        assert context["current_state"] == "状态: 备考\n"
        assert "专升本" in context["life_goals"]

    def test_events_and_candidates_are_not_in_any_injected_file(self, store):
        store.write_event("secret event detail", kind="other")
        store.add_pattern_candidate("unconfirmed hypothesis", ["e1", "e2"])
        context = store.read_context_files()
        joined = "\n".join(context.values())
        assert "secret event detail" not in joined
        assert "unconfirmed hypothesis" not in joined
