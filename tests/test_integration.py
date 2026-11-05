"""Live smoke test: explicitly opt in with a dedicated disposable workspace.

Set ANYTHINGLLM_LIVE_TEST=1, ANYTHINGLLM_TEST_URL, ANYTHINGLLM_TEST_API_KEY and
ANYTHINGLLM_TEST_WORKSPACE (must start with sync-test-). Never uses normal config.
"""
import os
import uuid
import pytest

from anythingllm_loader.anythingllm_api import AnythingLLM
from anythingllm_loader.config import AnythingLLMConfig
from anythingllm_loader.database import DocumentDatabase
from ingest_anythingllm_docs import sync_once

pytestmark = pytest.mark.skipif(os.getenv("ANYTHINGLLM_LIVE_TEST") != "1", reason="Live server tests require explicit opt-in")


def test_live_upload_embed_change_delete(tmp_path):
    url = os.environ["ANYTHINGLLM_TEST_URL"]
    key = os.environ["ANYTHINGLLM_TEST_API_KEY"]
    workspace = os.environ["ANYTHINGLLM_TEST_WORKSPACE"]
    assert workspace.startswith("sync-test-"), "Use a dedicated disposable sync-test-* workspace"
    source = tmp_path / "source"
    source.mkdir()
    doc = source / f"synthetic-{uuid.uuid4().hex}.txt"
    doc.write_text("Synthetic sync integration fixture, version one.")
    config = AnythingLLMConfig(key, [str(source)], [], [], workspace, url)
    api = AnythingLLM(config)
    db = DocumentDatabase(tmp_path / "live-state.db")
    db.initialize_database()
    before = set(api.fetch_embedded_workspace_documents())
    try:
        assert sync_once(config, db, api)
        first = db.get_documents()[0].anythingllm_document_location
        assert first in api.fetch_embedded_workspace_documents()
        doc.write_text("Synthetic sync integration fixture, version two.")
        assert sync_once(config, db, api)
        assert db.get_documents()[0].anythingllm_document_location != first
        doc.unlink()
        assert sync_once(config, db, api)
        assert not db.get_documents()
        assert before.issubset(api.fetch_embedded_workspace_documents())
    finally:
        # Cleanup only objects returned to this test's isolated SQLite journal.
        for owned in db.get_documents():
            location = owned.anythingllm_document_location
            try:
                api.unembed_document(location)
            finally:
                api.unload_document(location)
