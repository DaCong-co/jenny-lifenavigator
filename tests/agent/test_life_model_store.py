"""Tests for LifeModelStore — pure file I/O for the Life Model."""

import pytest

from jenny.life_model.store import LifeModelStore


@pytest.fixture
def store(tmp_path):
    return LifeModelStore(tmp_path)


class TestLifeModelStore:
    def test_read_context_files_keys(self, store):
        assert set(store.read_context_files()) == {
            "life_goals", "current_state", "patterns", "expression",
        }

    def test_empty_files_return_empty_strings(self, store):
        assert all(v == "" for v in store.read_context_files().values())

    def test_write_and_read_event(self, store):
        event_id = store.write_event("用户决定暂停某项目", kind="decision", source_turn="t1")
        refs = store.list_events()
        assert len(refs) == 1
        assert refs[0].kind == "decision"
        assert refs[0].first_line == "用户决定暂停某项目"
        assert store.read_event(event_id) == "用户决定暂停某项目"

    def test_write_event_rejects_empty(self, store):
        with pytest.raises(ValueError):
            store.write_event("", kind="other")

    def test_write_event_rejects_unknown_kind(self, store):
        with pytest.raises(ValueError):
            store.write_event("x", kind="bogus")

    def test_read_event_rejects_traversal(self, store):
        with pytest.raises(ValueError):
            store.read_event("../../etc/passwd")

    def test_update_current_state_is_snapshot(self, store):
        store.update_current_state("学习状态: 备考")
        store.update_current_state("学习状态: 冲刺")
        assert store.read_current_state() == "学习状态: 冲刺\n"

    def test_candidate_lifecycle(self, store):
        cid = store.add_pattern_candidate(
            "压力时倾向开启新项目", ["8月视频项目", "9月AI项目"], observed_period="8月~9月",
        )
        assert len(store.list_candidates()) == 1
        store.update_pattern_candidate_status(cid, "supported")
        assert store.list_candidates()[0].status == "supported"
        store.remove_pattern_candidate(cid)
        assert store.list_candidates() == []

    def test_add_candidate_is_idempotent(self, store):
        cid1 = store.add_pattern_candidate("same", ["a", "b"])
        cid2 = store.add_pattern_candidate("same", ["a", "b"])
        assert cid1 == cid2
        assert len(store.list_candidates()) == 1

    def test_remove_missing_candidate_raises(self, store):
        with pytest.raises(KeyError):
            store.remove_pattern_candidate("deadbeef")

    def test_update_status_rejects_unknown(self, store):
        with pytest.raises(ValueError):
            store.update_pattern_candidate_status("deadbeef", "bogus")

    def test_growth_notice_empty_when_small(self, store):
        assert store.growth_notice() is None
