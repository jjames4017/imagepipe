from pathlib import Path

import pytest

from imagepipe.config import ConfigError, load_config

ROOT = Path(__file__).resolve().parent.parent


def test_example_config_loads():
    cfg = load_config(ROOT / "config.example.yml")
    assert cfg.notify.events == ["finished", "error"]
    assert cfg.catalogue.list_archive_contents is True
    assert cfg.timezone == "Australia/Brisbane"


def test_yaml_on_key_gives_clear_error(tmp_path):
    p = tmp_path / "c.yml"
    p.write_text("manifest: /m.db\nlog_dir: /l\nreports_dir: /r\nnotify:\n  on: [finished]\n")
    with pytest.raises(ConfigError, match="YAML reads unquoted on/off"):
        load_config(p)


def test_unknown_key_rejected(tmp_path):
    p = tmp_path / "c.yml"
    p.write_text("manifest: /m.db\nlog_dir: /l\nreports_dir: /r\ncatalogue:\n  workerz: 2\n")
    with pytest.raises(ConfigError, match="workerz"):
        load_config(p)
