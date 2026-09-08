"""Tests for the life_model/ deny-list in resolve_allowed_path (v1.4)."""

import os

import pytest

from jenny.security.workspace_policy import WorkspaceBoundaryError, resolve_allowed_path


@pytest.fixture
def ws(tmp_path):
    (tmp_path / "life_model").mkdir()
    (tmp_path / "life_model" / "current_state.md").write_text("x", encoding="utf-8")
    (tmp_path / "other.txt").write_text("y", encoding="utf-8")
    return tmp_path


class TestLifeModelIsolation:
    def test_direct_deny(self, ws):
        with pytest.raises(WorkspaceBoundaryError):
            resolve_allowed_path(
                "life_model/current_state.md",
                workspace=ws, allowed_root=ws,
                denied_dirs=[str(ws / "life_model")],
            )

    def test_deny_beats_exact_allowlist(self, ws):
        with pytest.raises(WorkspaceBoundaryError):
            resolve_allowed_path(
                "life_model/current_state.md",
                workspace=ws, allowed_root=ws,
                extra_allowed_files=[str(ws / "life_model" / "current_state.md")],
                denied_dirs=[str(ws / "life_model")],
            )

    def test_unrelated_path_still_allowed(self, ws):
        p = resolve_allowed_path(
            "other.txt", workspace=ws, allowed_root=ws,
            denied_dirs=[str(ws / "life_model")],
        )
        assert p.name == "other.txt"

    def test_no_deny_preserves_old_behavior(self, ws):
        p = resolve_allowed_path(
            "life_model/current_state.md", workspace=ws, allowed_root=ws,
        )
        assert p.name == "current_state.md"

    def test_symlink_does_not_bypass_deny(self, ws):
        link = ws / "link"
        try:
            os.symlink(ws / "life_model", link)
        except OSError:
            pytest.skip("symlinks not supported on this platform")
        with pytest.raises(WorkspaceBoundaryError):
            resolve_allowed_path(
                "link/current_state.md",
                workspace=ws, allowed_root=ws,
                denied_dirs=[str(ws / "life_model")],
            )
