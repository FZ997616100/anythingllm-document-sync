"""AnythingLLM v1 API. A successful HTTP status alone is not ingestion proof."""
from pathlib import Path
from urllib.parse import quote
import requests


class APIError(RuntimeError):
    def __init__(self, message, documents=None):
        super().__init__(message)
        self.documents = documents or []


class AnythingLLM:
    def __init__(self, config):
        self.config = config
        self.timeout = getattr(config, "request_timeout", 120)

    def _request(self, method, endpoint, **kwargs):
        try:
            response = getattr(requests, method)(
                f"{self.config.anythingllm_url}/api/v1/{endpoint}",
                headers={"accept": "application/json", "Authorization": f"Bearer {self.config.api_key}"},
                timeout=self.timeout, **kwargs)
            if response.status_code != 200:
                # Some collectors return objects together with a processing error.
                # Journal their IDs even though the generation cannot be promoted.
                try:
                    partial = response.json().get("documents", [])
                    partial = [d for d in partial if isinstance(d, dict) and d.get("location")]
                except (ValueError, AttributeError, TypeError):
                    partial = []
                raise APIError(f"{endpoint}: HTTP {response.status_code}", partial)
            body = response.json()
            if not isinstance(body, dict):
                raise APIError(f"{endpoint}: invalid JSON object")
            return body
        except (requests.RequestException, ValueError) as exc:
            # Do not print response bodies: they can include document content.
            raise APIError(f"{endpoint}: {type(exc).__name__}") from exc

    @property
    def workspace_endpoint(self):
        return f"workspace/{quote(self.config.workspace_slug, safe='')}"

    def authenticate(self):
        return self._request("get", "auth").get("authenticated") is True

    @staticmethod
    def supported_file_types():
        return ['txt', 'md', 'org', 'adoc', 'rst', 'html', 'docx', 'odt', 'odp',
                'pdf', 'mbox', 'epub', 'xlsx', 'pptx']

    def upload_document(self, local_document_path):
        path = Path(local_document_path)
        if path.stat().st_size == 0 or path.suffix.lower().lstrip('.') not in self.supported_file_types():
            raise APIError("File is empty or unsupported")
        pdf = getattr(self.config, "pdf", {})
        if path.suffix.lower() == ".pdf" and pdf.get("mode", "off") != "off":
            from .pdf_preprocessor import extract_pdf_text, PdfExtractionError
            try:
                text = extract_pdf_text(path, **pdf)
            except PdfExtractionError as exc:
                raise APIError(f"PDF extraction failed: {exc}") from exc
            if not text.strip():
                raise APIError("PDF preprocessing returned no text")
            body = self._request("post", "document/raw-text", json={
                "textContent": text,
                "metadata": {"title": path.name, "docSource": "Local PDF text/OCR",
                             "chunkSource": path.resolve().as_uri(), "url": path.resolve().as_uri()},
            })
        else:
            with path.open("rb") as source:
                body = self._request("post", "document/upload", files={"file": (path.name, source)})
        documents = body.get("documents", [])
        valid = [d for d in documents if isinstance(d, dict) and isinstance(d.get("location"), str)
                 and d["location"]] if isinstance(documents, list) else []
        if body.get("success") is not True or body.get("error") or not valid or len(valid) != len(documents):
            raise APIError("Upload did not return a complete successful document list", valid)
        return valid

    def fetch_loaded_documents_from_anythingllm(self):
        loaded = []
        for item in self._request("get", "documents")["localFiles"]["items"]:
            self.parse_loaded_document(item, loaded)
        return loaded

    def parse_loaded_document(self, item, loaded_documents):
        if item["type"] == "folder":
            for child in item["items"]:
                self.parse_loaded_document(child, loaded_documents)
        elif item["type"] == "file":
            loaded_documents.append(item["name"])

    def fetch_embedded_workspace_documents(self):
        workspace = self._request("get", self.workspace_endpoint).get("workspace")
        if isinstance(workspace, list):
            if not workspace:
                raise APIError("Configured workspace does not exist")
            workspace = workspace[0]
        if not isinstance(workspace, dict) or not isinstance(workspace.get("documents"), list):
            raise APIError("Workspace response has no document membership list")
        documents = workspace["documents"]
        if any(not isinstance(doc, dict) or not isinstance(doc.get("docpath"), str) for doc in documents):
            raise APIError("Workspace response contains invalid document membership")
        return list({doc["docpath"] for doc in documents})

    def _update_embeddings(self, key, location):
        body = self._request("post", self.workspace_endpoint + "/update-embeddings",
                             json={key: [location]})
        if body.get("errors") or body.get("error") or body.get("success") is False:
            raise APIError("Embedding update reported errors")
        present = location in self.fetch_embedded_workspace_documents()
        if present != (key == "adds"):
            raise APIError("Workspace membership did not confirm embedding update")
        return True

    def embed_new_document(self, document_to_embed):
        return self._update_embeddings("adds", document_to_embed)

    def unembed_document(self, document_to_unembed):
        return self._update_embeddings("deletes", document_to_unembed)

    def unload_document(self, document_to_unload):
        body = self._request("delete", "system/remove-documents", json={"names": [document_to_unload]})
        if body.get("error") or body.get("errors") or body.get("success") is False:
            raise APIError("Document removal reported errors")
        return True
