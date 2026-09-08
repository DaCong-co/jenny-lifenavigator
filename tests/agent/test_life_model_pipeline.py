"""End-to-end Life Model flow at the store level."""

import pytest

from jenny.life_model.store import LifeModelStore


@pytest.fixture
def store(tmp_path):
    return LifeModelStore(tmp_path)


class TestLifeModelPipeline:
    def test_record_list_update_propose_flow(self, store):
        # 1. the agent records a salient event
        event_id = store.write_event("用户决定优先准备专升本", kind="decision")
        # 2. Dream lists events
        refs = store.list_events()
        assert len(refs) == 1
        assert refs[0].kind == "decision"
        assert store.read_event(event_id) == "用户决定优先准备专升本"

        # 3. Dream updates current_state (snapshot)
        store.update_current_state("学习状态: 备考中\n压力: 偏高")
        # 4. Dream proposes a candidate
        cid = store.add_pattern_candidate(
            "压力时倾向开启新项目", ["8月视频项目", "9月AI项目"],
            observed_period="8月~9月", confidence="medium",
        )
        assert cid

        # 5. the injected context carries the 4 files, not events/candidates
        context = store.read_context_files()
        assert context["current_state"] == "学习状态: 备考中\n压力: 偏高\n"
        assert "用户决定优先准备专升本" not in context["current_state"]
        assert "压力时倾向开启新项目" not in "\n".join(context.values())

        # events and candidates remain in their own stores
        assert len(store.list_events()) == 1
        assert len(store.list_candidates()) == 1
