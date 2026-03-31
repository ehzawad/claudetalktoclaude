"""Tests for install_hooks.py — hook merging, idempotency, preservation."""

import json
import tempfile
from pathlib import Path

from chronicle.install_hooks import install_hooks, CHRONICLE_HOOKS, _has_chronicle_hook


def _install_and_read(existing_settings: dict | None = None) -> dict:
    """Helper: write existing settings, run install_hooks, return result."""
    with tempfile.TemporaryDirectory() as tmpdir:
        path = Path(tmpdir) / "settings.json"
        if existing_settings is not None:
            path.write_text(json.dumps(existing_settings))
        # If existing_settings is None, file doesn't exist — tests fresh install

        install_hooks(str(path))
        return json.loads(path.read_text())


class TestHookMerging:
    def test_fresh_install_creates_all_events(self):
        result = _install_and_read()
        hooks = result["hooks"]
        for event in CHRONICLE_HOOKS:
            assert event in hooks
            assert len(hooks[event]) >= 1

    def test_preserves_existing_user_hooks(self):
        user_hook = {
            "matcher": "*.py",
            "hooks": [{"type": "command", "command": "my-formatter"}],
        }
        existing = {"hooks": {"SessionStart": [user_hook]}}
        result = _install_and_read(existing)

        hooks = result["hooks"]["SessionStart"]
        # Should have both the user's hook AND chronicle's hook
        assert len(hooks) == 2
        # User's hook should be first (preserved position)
        assert hooks[0]["hooks"][0]["command"] == "my-formatter"
        # Chronicle's hook should be appended
        assert hooks[1]["hooks"][0]["command"] == "chronicle-hook"

    def test_idempotent_reinstall(self):
        """Running install twice should not duplicate chronicle hooks."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump({}, f)
            path = f.name

        install_hooks(path)
        install_hooks(path)  # second run

        with open(path) as f:
            result = json.load(f)

        for event in CHRONICLE_HOOKS:
            chronicle_entries = [
                mg for mg in result["hooks"][event]
                if _has_chronicle_hook(mg)
            ]
            assert len(chronicle_entries) == 1, f"Duplicate chronicle hooks in {event}"

    def test_preserves_non_hook_settings(self):
        existing = {"theme": "dark", "fontSize": 14}
        result = _install_and_read(existing)
        assert result["theme"] == "dark"
        assert result["fontSize"] == 14

    def test_preserves_hooks_on_other_events(self):
        """Events not used by Chronicle should be untouched."""
        existing = {
            "hooks": {
                "PreToolUse": [
                    {"matcher": "", "hooks": [{"type": "command", "command": "my-guard"}]}
                ]
            }
        }
        result = _install_and_read(existing)
        assert len(result["hooks"]["PreToolUse"]) == 1
        assert result["hooks"]["PreToolUse"][0]["hooks"][0]["command"] == "my-guard"

    def test_has_chronicle_hook_helper(self):
        assert _has_chronicle_hook({"hooks": [{"command": "chronicle-hook"}]})
        assert not _has_chronicle_hook({"hooks": [{"command": "other-tool"}]})
        assert not _has_chronicle_hook({"hooks": []})
        assert not _has_chronicle_hook({})
