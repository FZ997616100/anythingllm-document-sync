from pathlib import Path

import pytest

from anythingllm_loader.config import AnythingLLMConfig


def test_utf8_bom_and_relative_chinese_paths(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("api-key: test-key\nworkspace-slug: test\nfile-paths: ['资料']\npdf: {mode: auto, model_dir: models}\n", encoding="utf-8-sig")
    config = AnythingLLMConfig.load_config(path)
    assert Path(config.file_paths[0]) == tmp_path / "资料"
    assert config.directory_excludes == []
    assert Path(config.pdf["model_dir"]) == tmp_path / "models"
    assert config.anythingllm_url == "http://localhost:3001"


@pytest.mark.parametrize("replacement", ["file-paths: []", "file-paths: /wrong", "file-paths: [null]", "pdf: {mode: wrong}", "pdf: {mode: off}", "pdf: {typo: 1}", "anythingllm-url: file:///tmp", "api-key: ''"])
def test_invalid_config_fails_before_sync(tmp_path, replacement):
    path = tmp_path / "config.yaml"
    base = {"api-key": "test-key", "workspace-slug": "test", "file-paths": [str(tmp_path)]}
    import yaml
    base.update(yaml.safe_load(replacement))
    path.write_text(yaml.safe_dump(base), encoding="utf-8")
    with pytest.raises(ValueError):
        AnythingLLMConfig.load_config(path)


def test_partial_yaml_save_is_a_retryable_error_without_key_leak(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("api-key: do-not-print-this\nfile-paths: [", encoding="utf-8")
    with pytest.raises(ValueError, match="Invalid YAML") as error:
        AnythingLLMConfig.load_config(path)
    assert "do-not-print-this" not in str(error.value)
