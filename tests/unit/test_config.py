"""Unit tests for chronicle.config.load_config() defensive behavior.

A malformed ~/.chronicle/config.json must NOT crash the hook, daemon,
batch, or doctor — it must return DEFAULT_CONFIG with a `_load_error`
key so `chronicle doctor` can surface the problem.
"""
from __future__ import annotations

import pytest


def test_missing_config_returns_defaults(chronicle_env):
    from chronicle import config
    cfg = config.load_config()
    assert cfg["processing_mode"] == "foreground"
    assert cfg["max_retries"] == 3
    assert "_load_error" not in cfg


def test_valid_config_merges_over_defaults(chronicle_env):
    from chronicle import config
    import json
    config.config_file().write_text(json.dumps({
        "max_retries": 7, "model": "custom",
    }))
    cfg = config.load_config()
    assert cfg["max_retries"] == 7
    assert cfg["model"] == "custom"
    # Unset keys still come from defaults
    assert cfg["fallback_model"] == "sonnet"
    assert "_load_error" not in cfg


def test_malformed_json_returns_defaults_with_error(chronicle_env):
    from chronicle import config
    config.config_file().write_text('{"not_valid": ,}\n')  # trailing comma
    cfg = config.load_config()
    # Must NOT raise. Must return defaults + _load_error.
    assert cfg["processing_mode"] == "foreground"
    assert cfg["max_retries"] == 3
    assert "_load_error" in cfg
    assert "config.json" in cfg["_load_error"]


def test_non_object_json_returns_defaults_with_error(chronicle_env):
    from chronicle import config
    config.config_file().write_text('"just a string"\n')
    cfg = config.load_config()
    assert cfg["processing_mode"] == "foreground"
    assert "_load_error" in cfg


def test_doctor_surfaces_config_error(chronicle_env):
    """The `chronicle doctor` diagnostic should flag the broken config
    prominently so the user knows why things are misbehaving."""
    from chronicle import config, doctor
    config.config_file().write_text('{"trailing": ,\n')  # malformed
    data = doctor.collect_diagnostics()
    assert data["ok"] is False
    msgs = " ".join(data["drift_warnings"])
    assert "config load error" in msgs.lower()
