"""Offline API contract checks. No configured server or user source is accessed."""
from unittest.mock import Mock, patch
import pytest
import requests

from anythingllm_loader.anythingllm_api import AnythingLLM, APIError
from anythingllm_loader.config import AnythingLLMConfig


@pytest.fixture
def config():
    return AnythingLLMConfig("synthetic-key", [], [], [], "isolated-test", "http://localhost:3001")


@pytest.fixture
def api(config):
    return AnythingLLM(config)


def response(body, status=200):
    result = Mock(status_code=status)
    result.json.return_value = body
    return result


def test_upload_returns_every_sheet_with_case_insensitive_extension(api, tmp_path):
    source = tmp_path / "book.XLSX"
    source.write_bytes(b"synthetic upload payload")
    docs = [{"location": "custom-documents/sheet1.json"}, {"location": "custom-documents/sheet2.json"}]
    with patch("requests.post", return_value=response({"success": True, "documents": docs})) as post:
        assert api.upload_document(source) == docs
    assert post.call_args.kwargs["timeout"] == 120
    assert post.call_args.kwargs["files"]["file"][0] == "book.XLSX"
    assert "pptx" in api.supported_file_types()


@pytest.mark.parametrize("body", [
    {"success": False, "documents": [{"location": "custom-documents/partial.json"}]},
    {"success": True, "documents": [{"location": "custom-documents/partial.json"}, {}]},
])
def test_partial_upload_keeps_returned_ownership(api, tmp_path, body):
    source = tmp_path / "book.xlsx"
    source.write_bytes(b"data")
    with patch("requests.post", return_value=response(body)), pytest.raises(APIError) as caught:
        api.upload_document(source)
    assert caught.value.documents == [{"location": "custom-documents/partial.json"}]


@pytest.mark.parametrize("body", [{"errors": ["embedding failed"]}, {"success": False}, {"error": "failed"}])
def test_embedding_errors_are_not_success(api, body):
    with patch("requests.post", return_value=response(body)), pytest.raises(APIError):
        api.embed_new_document("custom-documents/new.json")


def test_embedding_http_200_requires_actual_membership(api):
    with patch("requests.post", return_value=response({"errors": []})), \
         patch("requests.get", return_value=response({"workspace": [{"documents": []}]})), \
         pytest.raises(APIError, match="membership"):
        api.embed_new_document("custom-documents/new.json")


def test_embedding_and_unembedding_confirm_membership(api):
    location = "custom-documents/new.json"
    with patch("requests.post", return_value=response({"errors": []})) as post, \
         patch("requests.get", return_value=response({"workspace": [{"documents": [{"docpath": location}]}]})):
        assert api.embed_new_document(location)
        assert post.call_args.kwargs["json"] == {"adds": [location]}
    with patch("requests.post", return_value=response({})), \
         patch("requests.get", return_value=response({"workspace": [{"documents": []}]})):
        assert api.unembed_document(location)


def test_timeout_does_not_leak_response_or_key(api):
    with patch("requests.get", side_effect=requests.Timeout("secret internal detail")), pytest.raises(APIError) as caught:
        api.authenticate()
    assert "secret" not in str(caught.value)


def test_delete_errors_and_endpoint(api):
    with patch("requests.delete", return_value=response({"success": True})) as delete:
        assert api.unload_document("custom-documents/owned.json")
    assert delete.call_args.args[0].endswith("/api/v1/system/remove-documents")
    assert delete.call_args.kwargs["json"] == {"names": ["custom-documents/owned.json"]}
    with patch("requests.delete", return_value=response({"errors": ["failed"]})), pytest.raises(APIError):
        api.unload_document("custom-documents/owned.json")


def test_pdf_preprocessing_upload_retains_source_metadata(api, config, tmp_path):
    source = tmp_path / "original.PDF"
    source.write_bytes(b"synthetic pdf")
    config.pdf = {"mode": "auto"}
    with patch("anythingllm_loader.pdf_preprocessor.extract_pdf_text", return_value="page 1 text"), \
         patch("requests.post", return_value=response({"success": True, "documents": [{"location": "custom-documents/pdf.json"}]})) as post:
        api.upload_document(source)
    assert post.call_args.args[0].endswith("/api/v1/document/raw-text")
    metadata = post.call_args.kwargs["json"]["metadata"]
    assert metadata["title"] == source.name
    assert metadata["chunkSource"] == source.resolve().as_uri()


def test_pdf_extraction_failure_is_retryable_api_error(api, config, tmp_path):
    from anythingllm_loader.pdf_preprocessor import PdfExtractionError
    source = tmp_path / "original.pdf"
    source.write_bytes(b"synthetic pdf")
    config.pdf = {"mode": "auto"}
    with patch("anythingllm_loader.pdf_preprocessor.extract_pdf_text", side_effect=PdfExtractionError("missing OCR")), \
         patch("requests.post") as post, pytest.raises(APIError, match="PDF extraction failed"):
        api.upload_document(source)
    post.assert_not_called()


def test_non_200_upload_keeps_partial_objects(api, tmp_path):
    source = tmp_path / "file.txt"
    source.write_text("synthetic")
    docs = [{"location": "custom-documents/partial.json"}]
    with patch("requests.post", return_value=response({"documents": docs}, status=500)), \
         pytest.raises(APIError) as caught:
        api.upload_document(source)
    assert caught.value.documents == docs


@pytest.mark.parametrize("body", [
    {"workspace": []}, {"workspace": None}, {},
    {"workspace": [{}]}, {"workspace": [{"documents": None}]},
    {"workspace": [{"documents": [{}]}]},
])
def test_missing_or_malformed_workspace_raises_retryable_api_error(api, body):
    with patch("requests.get", return_value=response(body)), pytest.raises(APIError):
        api.fetch_embedded_workspace_documents()
