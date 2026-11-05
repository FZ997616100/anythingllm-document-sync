"""Synthetic filesystem + real SQLite exercise the production sync orchestration."""
import os
import sqlite3
from unittest.mock import patch
import pytest

from anythingllm_loader.anythingllm_api import APIError
from anythingllm_loader.config import AnythingLLMConfig
from anythingllm_loader.database import DocumentDatabase, document_path_key
from ingest_anythingllm_docs import sync_once, fetch_local_documents, state_lock, main


class Remote:
    def __init__(self):
        self.embedded = {"custom-documents/manual.json"}
        self.uploaded = set(self.embedded)
        self.events = []
        self.fail_embed = set()
        self.fail_delete = set()
        self.sheets = 1
        self.serial = 0
        self.partial = False

    def authenticate(self):
        return True

    def upload_document(self, path):
        self.serial += 1
        docs = [{"location": f"custom-documents/{self.serial}-{sheet}.json", "pageContent": "private text"}
                for sheet in range(self.sheets)]
        self.uploaded.update(d["location"] for d in docs)
        self.events.append(("upload", path))
        if self.partial:
            raise APIError("partial upload", docs)
        return docs

    def fetch_embedded_workspace_documents(self):
        return list(self.embedded)

    def embed_new_document(self, location):
        self.events.append(("embed", location))
        if location in self.fail_embed:
            raise APIError("synthetic embedding failure")
        self.embedded.add(location)
        return True

    def unembed_document(self, location):
        self.events.append(("unembed", location))
        self.embedded.discard(location)
        return True

    def unload_document(self, location):
        self.events.append(("delete", location))
        if location in self.fail_delete:
            raise APIError("synthetic delete failure")
        self.uploaded.discard(location)
        return True


@pytest.fixture
def setup(tmp_path):
    root = tmp_path / "source"
    root.mkdir()
    config = AnythingLLMConfig("synthetic-key", [str(root)], [], [], "isolated-test")
    db = DocumentDatabase(tmp_path / "state.sqlite")
    db.initialize_database()
    return root, config, db, Remote()


def test_recursive_same_names_and_case_extensions(setup):
    root, config, db, api = setup
    for sub in ["one", "two"]:
        (root / sub).mkdir()
        (root / sub / "same.XLSX").write_bytes(sub.encode())
    (root / "deck.PPTX").write_bytes(b"deck")
    api.sheets = 2
    assert sync_once(config, db, api)
    assert len(db.get_documents()) == 6
    assert len({d.local_file_path for d in db.get_documents()}) == 3
    assert all(d.status == "active" and "pageContent" not in d.content for d in db.get_documents())
    serial = api.serial
    assert sync_once(config, db, api)
    assert api.serial == serial


def test_content_change_detected_with_same_size_and_restored_mtime(setup):
    root, config, db, api = setup
    source = root / "file.txt"
    source.write_text("old")
    original_stat = source.stat()
    assert sync_once(config, db, api)
    old = db.get_documents()[0].anythingllm_document_location
    source.write_text("new")
    os.utime(source, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))
    assert sync_once(config, db, api)
    assert old not in api.uploaded
    assert api.serial == 2
    events = [kind for kind, value in api.events]
    assert events[-4:] == ["upload", "embed", "unembed", "delete"]


def test_failed_sheet_preserves_old_and_retries_without_reupload(setup):
    root, config, db, api = setup
    source = root / "book.xlsx"
    source.write_bytes(b"old")
    api.sheets = 2
    assert sync_once(config, db, api)
    old = set(api.uploaded)
    source.write_bytes(b"new")
    api.fail_embed.add("custom-documents/2-1.json")
    assert not sync_once(config, db, api)
    assert old.issubset(api.uploaded)
    assert len(db.get_documents()) == 4
    assert not any(event == "delete" for event, value in api.events)
    api.fail_embed.clear()
    assert sync_once(config, db, api)
    assert api.serial == 2
    assert len(db.get_documents()) == 2
    assert "custom-documents/manual.json" in api.embedded


def test_partial_upload_is_tracked_then_replaced(setup):
    root, config, db, api = setup
    (root / "file.txt").write_text("source")
    api.partial = True
    assert not sync_once(config, db, api)
    assert len(db.get_documents()) == 1
    assert db.get_documents()[0].content_hash is None
    api.partial = False
    assert sync_once(config, db, api)
    assert len(db.get_documents()) == 1
    assert "custom-documents/1-0.json" not in api.uploaded


def test_concurrent_edit_invalidates_pending_even_when_source_changes_back(setup):
    root, config, db, api = setup
    source = root / "file.txt"
    source.write_text("original")
    upload = api.upload_document
    def edit_during_upload(path):
        result = upload(path)
        source.write_text("changed during upload")
        return result
    with patch.object(api, "upload_document", side_effect=edit_during_upload):
        assert not sync_once(config, db, api)
    assert db.get_documents()[0].content_hash is None
    source.write_text("original")
    assert sync_once(config, db, api)
    assert api.serial == 2
    assert len(db.get_documents()) == 1


def test_one_upload_failure_does_not_block_other_files_or_remove_old(setup):
    root, config, db, api = setup
    bad = root / "bad.pdf"
    bad.write_text("original synthetic pdf")
    assert sync_once(config, db, api)
    old_location = db.get_documents()[0].anythingllm_document_location
    bad.write_text("changed synthetic pdf")
    good = root / "good.txt"
    good.write_text("synthetic text")
    upload = api.upload_document
    def failed_pdf(path):
        if path == str(bad):
            raise APIError("synthetic PDF extraction failure")
        return upload(path)
    with patch.object(api, "upload_document", side_effect=failed_pdf):
        assert not sync_once(config, db, api)
    assert old_location in api.embedded
    assert len(db.get_documents()) == 2
    assert all(d.status == "active" for d in db.get_documents())


def test_failed_delete_stays_in_state_for_next_cycle(setup):
    root, config, db, api = setup
    source = root / "file.txt"
    source.write_text("source")
    assert sync_once(config, db, api)
    location = db.get_documents()[0].anythingllm_document_location
    source.unlink()
    api.fail_delete.add(location)
    assert not sync_once(config, db, api)
    assert len(db.get_documents()) == 1
    api.fail_delete.clear()
    assert sync_once(config, db, api)
    assert not db.get_documents()
    assert api.embedded == {"custom-documents/manual.json"}


@pytest.mark.parametrize("change", ["missing-root", "removed-config", "excluded-file", "excluded-directory", "scan-error"])
def test_scope_changes_and_scan_failures_do_not_delete(setup, change):
    root, config, db, api = setup
    source = root / "file.txt"
    source.write_text("source")
    assert sync_once(config, db, api)
    if change == "missing-root":
        source.unlink()
        root.rmdir()
    elif change == "removed-config":
        config.file_paths = []
    elif change == "excluded-file":
        config.file_excludes = ["file.txt"]
    elif change == "excluded-directory":
        config.directory_excludes = [str(root)]
    if change == "scan-error":
        with patch("ingest_anythingllm_docs.os.walk", side_effect=PermissionError("denied")):
            assert not sync_once(config, db, api)
    else:
        sync_once(config, db, api)
    assert len(db.get_documents()) == 1
    assert not any(event == "delete" for event, value in api.events)


def test_changed_workspace_does_not_claim_old_objects(setup):
    root, config, db, api = setup
    source = root / "file.txt"
    source.write_text("source")
    assert sync_once(config, db, api)
    old = db.get_documents()[0].anythingllm_document_location
    source.unlink()
    config.workspace_slug = "other-workspace"
    assert sync_once(config, db, api)
    assert old in api.uploaded
    assert len(db.get_documents()) == 1


def test_migration_preserves_legacy_rows_and_adds_columns(tmp_path):
    state = tmp_path / "legacy.db"
    with sqlite3.connect(state) as conn:
        conn.execute("CREATE TABLE documents (id INTEGER PRIMARY KEY, local_file_path TEXT, upload_timestamp DATETIME, anythingllm_document_location TEXT, content TEXT)")
        conn.execute("INSERT INTO documents VALUES (1, '/old/file.txt', '2020-01-01 10:00:00', 'custom-documents/old.json', '{}')")
    db = DocumentDatabase(state)
    db.initialize_database()
    db.initialize_database()
    docs = db.get_documents()
    assert len(docs) == 1
    assert docs[0].status == "active"
    assert docs[0].generation == "legacy"
    assert docs[0].anythingllm_document_location == "custom-documents/old.json"
    assert docs[0].path_key == document_path_key("/old/file.txt")


@pytest.mark.parametrize("initial, changed", [
    ({"mode": "off"}, {"mode": "auto"}),
    ({"mode": "auto"}, {"mode": "force"}),
    ({"mode": "auto"}, {"mode": "auto", "languages": "eng"}),
    ({"mode": "auto"}, {"mode": "auto", "dpi": 200}),
    ({"mode": "auto"}, {"mode": "auto", "min_text_chars": 50}),
    ({"mode": "auto"}, {"mode": "auto", "tesseract_cmd": "other-local-tesseract"}),
    ({"mode": "auto"}, {"mode": "auto", "tessdata_dir": "other-local-data"}),
])
def test_pdf_processing_settings_trigger_reupload_and_preserve_old_on_failure(setup, initial, changed):
    root, config, db, api = setup
    source = root / "file.PDF"
    source.write_bytes(b"unchanged synthetic pdf")
    config.pdf = initial
    assert sync_once(config, db, api)
    old = db.get_documents()[0].anythingllm_document_location
    old_hash = db.get_documents()[0].content_hash
    config.pdf = changed
    with patch.object(api, "upload_document", side_effect=APIError("synthetic processing failure")):
        assert not sync_once(config, db, api)
    assert old in api.embedded
    assert len(db.get_documents()) == 1
    assert db.get_documents()[0].content_hash == old_hash
    assert sync_once(config, db, api)
    assert old not in api.uploaded
    assert api.serial == 2
    assert len(db.get_documents()) == 1
    assert db.get_documents()[0].content_hash != old_hash


def test_pdf_explicit_default_options_keep_same_effective_fingerprint(setup):
    root, config, db, api = setup
    (root / "file.pdf").write_bytes(b"synthetic pdf")
    config.pdf = {"mode": "auto"}
    assert sync_once(config, db, api)
    config.pdf = {"mode": "auto", "languages": "chi_sim+eng", "dpi": 300}
    assert sync_once(config, db, api)
    assert api.serial == 1


@pytest.mark.skipif(os.name != "nt", reason="Windows case-insensitive path identity regression")
def test_windows_root_case_change_preserves_old_on_upload_failure_then_replaces(setup):
    root, config, db, api = setup
    source = root / "file.txt"
    source.write_text("old synthetic content")
    config.file_paths = [str(root).upper()]
    assert sync_once(config, db, api)
    old = db.get_documents()[0].anythingllm_document_location
    config.file_paths = [str(root).lower()]
    source.write_text("changed synthetic content")
    with patch.object(api, "upload_document", side_effect=APIError("upload offline")):
        assert not sync_once(config, db, api)
    assert len(db.get_documents()) == 1
    assert old in api.embedded
    assert not any(event == "delete" for event, value in api.events)
    assert sync_once(config, db, api)
    assert len(db.get_documents()) == 1
    assert old not in api.uploaded
    assert db.get_documents()[0].status == "active"


@pytest.mark.skipif(os.name != "nt", reason="Windows legacy path identity migration")
def test_legacy_windows_path_case_is_migrated_and_activated_together(tmp_path):
    state = tmp_path / "legacy.db"
    source = tmp_path / "Source" / "file.txt"
    source.parent.mkdir()
    source.write_text("synthetic")
    with sqlite3.connect(state) as conn:
        conn.execute("CREATE TABLE documents (id INTEGER PRIMARY KEY, local_file_path TEXT, upload_timestamp DATETIME, anythingllm_document_location TEXT, content TEXT)")
        conn.execute("INSERT INTO documents VALUES (1, ?, '2020-01-01 10:00:00', 'custom-documents/old.json', '{}')", (str(source).upper(),))
    db = DocumentDatabase(state)
    db.initialize_database()
    config = AnythingLLMConfig("synthetic-key", [str(source.parent).lower()], [], [], "isolated-test")
    api = Remote()
    api.uploaded.add("custom-documents/old.json")
    api.embedded.add("custom-documents/old.json")
    assert sync_once(config, db, api)
    assert len(db.get_documents()) == 1
    assert db.get_documents()[0].status == "active"
    assert "custom-documents/old.json" not in api.uploaded


@pytest.mark.skipif(os.name != "nt", reason="Windows exclusions must use the same path identity")
def test_windows_changed_case_and_exclusion_preserve_owned_document(setup):
    root, config, db, api = setup
    source = root / "file.txt"
    source.write_text("synthetic content")
    config.file_paths = [str(root).upper()]
    assert sync_once(config, db, api)
    old = db.get_documents()[0].anythingllm_document_location
    config.file_paths = [str(root).lower()]
    config.directory_excludes = [str(root).lower()]
    assert sync_once(config, db, api)
    assert len(db.get_documents()) == 1
    assert old in api.embedded
    assert not any(event == "delete" for event, value in api.events)


def test_missing_workspace_is_checked_before_upload(setup):
    root, config, db, api = setup
    (root / "file.txt").write_text("synthetic content")
    with patch.object(api, "fetch_embedded_workspace_documents", side_effect=APIError("Workspace does not exist")), \
         pytest.raises(APIError, match="Workspace does not exist"):
        sync_once(config, db, api)
    assert not api.events
    assert not db.get_documents()


def test_state_lock_is_exclusive_and_reusable(tmp_path):
    state = tmp_path / "state.db"
    with state_lock(state), pytest.raises(RuntimeError, match="Another sync"):
        with state_lock(state):
            pass
    with state_lock(state):
        pass


def test_cli_single_run_and_watch_use_explicit_paths(setup, tmp_path):
    root, config, db, api = setup
    state = tmp_path / "cli.db"
    with patch.object(AnythingLLMConfig, "load_config", return_value=config) as load, \
         patch("ingest_anythingllm_docs.sync_once", return_value=True) as sync:
        assert main(["--config", "synthetic.yaml", "--state", str(state)]) == 0
        assert str(load.call_args.args[0]) == "synthetic.yaml"
        assert sync.call_count == 1
    with patch.object(AnythingLLMConfig, "load_config", return_value=config), \
         patch("ingest_anythingllm_docs.sync_once", return_value=True) as sync, \
         patch("ingest_anythingllm_docs.time.sleep", side_effect=[None, KeyboardInterrupt]) as sleep:
        assert main(["--watch", "--interval", "60", "--state", str(state)]) == 0
        assert sync.call_count == 2
        sleep.assert_called_with(60)
