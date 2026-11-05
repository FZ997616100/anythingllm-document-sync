import pathlib
from urllib.parse import urlparse

import yaml

CONFIG_DIR = pathlib.Path.home() / ".anythingllm-sync"
CONFIG_FILE = 'config.yaml'


DEFAULT_URL = "http://localhost:3001"

class AnythingLLMConfig:

    # create init method including all config keys
    def __init__(self, api_key: str, file_paths: list, directory_excludes: list, file_excludes: list,
                 workspace_slug: str, anythingllm_url: str = DEFAULT_URL, pdf: dict | None = None):
        self.api_key = api_key
        self.file_paths = file_paths
        self.directory_excludes = directory_excludes
        self.file_excludes = file_excludes
        self.workspace_slug = workspace_slug
        self.anythingllm_url = anythingllm_url.rstrip("/")
        self.pdf = dict(pdf or {})
        self.pdf.setdefault("mode", "off")

    @staticmethod
    def load_config(path=None):
        config_path = pathlib.Path(path).expanduser() if path else CONFIG_DIR / CONFIG_FILE
        with config_path.open("r", encoding="utf-8-sig") as stream:
            try:
                config = yaml.safe_load(stream)
            except yaml.YAMLError as exc:
                # YAML diagnostics may contain the API-key line.
                raise ValueError("Invalid YAML configuration; check syntax and indentation") from exc
        if not isinstance(config, dict):
            raise ValueError("Configuration must be a YAML mapping")
        for key in ("api-key", "workspace-slug"):
            if not isinstance(config.get(key), str) or not config[key].strip():
                raise ValueError(f"{key} must be a non-empty string")
        for key in ("file-paths", "directory-excludes", "file-excludes"):
            values = config.get(key, [] if key != "file-paths" else None)
            if not isinstance(values, list) or any(not isinstance(v, str) or not v.strip() for v in values):
                raise ValueError(f"{key} must be a list of non-empty strings")
            config[key] = values
        if not config["file-paths"]:
            raise ValueError("file-paths must contain at least one source directory")
        url = config.get("anythingllm-url", DEFAULT_URL)
        parsed = urlparse(url) if isinstance(url, str) else None
        if not parsed or parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError("anythingllm-url must be an HTTP(S) URL without credentials")
        pdf = config.get("pdf", {})
        allowed = {"mode", "min_text_chars", "dpi", "model_dir"}
        if not isinstance(pdf, dict) or set(pdf) - allowed:
            raise ValueError("pdf must be a mapping containing only documented PDF options")
        if pdf.get("mode", "off") not in ("off", "auto", "force"):
            raise ValueError("pdf.mode must be off, auto or force (quote 'off' in YAML)")
        if pdf.get("model_dir") is not None:
            if not isinstance(pdf["model_dir"], str) or not pdf["model_dir"].strip():
                raise ValueError("pdf.model_dir must be a directory path or null")
            models = pathlib.Path(pdf["model_dir"]).expanduser()
            if not models.is_absolute():
                models = config_path.resolve().parent / models
            pdf["model_dir"] = str(models.absolute())
        file_paths = []
        for value in config["file-paths"]:
            source = pathlib.Path(value).expanduser()
            if not source.is_absolute():
                source = config_path.resolve().parent / source
            file_paths.append(str(source.absolute()))
        return AnythingLLMConfig(config["api-key"], file_paths, config["directory-excludes"],
                                 config["file-excludes"], config["workspace-slug"], url, pdf)
