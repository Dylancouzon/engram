"""config.toml is hand-edited, so a mistyped number must coerce (or fail
loudly), never silently become a string that breaks the first write."""

import pytest

from engram.config import Config


def test_mistyped_float_is_coerced(tmp_path):
    (tmp_path / "config.toml").write_text('salience_floor = "0.8"\n')
    cfg = Config.load(tmp_path)
    assert cfg.salience_floor == 0.8 and isinstance(cfg.salience_floor, float)


def test_uncoercible_value_raises_clearly(tmp_path):
    (tmp_path / "config.toml").write_text('recall_k = "lots"\n')
    with pytest.raises(ValueError, match="recall_k"):
        Config.load(tmp_path)


def test_native_types_pass_through(tmp_path):
    (tmp_path / "config.toml").write_text(
        "salience_floor = 0.2\nredaction_enabled = false\n")
    cfg = Config.load(tmp_path)
    assert cfg.salience_floor == 0.2 and cfg.redaction_enabled is False
