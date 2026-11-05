# AnythingLLM Document Sync

A fork of [dastra/anythingllm-document-sync](https://github.com/dastra/anythingllm-document-sync)
with Windows polling, XLSX/PPTX support and optional local PDF OCR.
[中文说明与验收边界](docs/README.zh-CN.md).

## Changes

- Recursively scan all configured source directories; extensions are case-insensitive.
- Track every returned document (including every nonempty XLSX worksheet).
- Hash file content; retry incomplete uploads/embeddings/cleanup.
- Confirm the complete new generation in the workspace before removing the old one.
- Only delete objects recorded by this tool, leaving manual uploads alone.
- Missing roots or directory traversal errors suppress deletion.
- Removed/excluded roots are left alone; renames are new uploads plus old-path cleanup.
- Optional PDF preprocessing extracts text per page or OCRs entire rendered pages.
- Changes to PDF extraction settings automatically trigger reprocessing.

This is a companion to AnythingLLM, not an installer or a replacement UI.

## Setup

Requires Python 3.12/3.13 and a running AnythingLLM Desktop/server.
SQLite is included with Python; pyenv is not required.

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item -LiteralPath config.example.yaml -Destination config.local.yaml
```

Fill in a local developer API key, workspace slug and source roots.
Use `http://127.0.0.1:3001` for a same-machine Desktop installation.
See [config.example.yaml](config.example.yaml). Never commit a real key.
Relative source paths resolve against the YAML directory.
Exclusions retain the upstream substring matching semantics.

```powershell
.\.venv\Scripts\python.exe ingest_anythingllm_docs.py --config config.local.yaml --state sync.db
.\.venv\Scripts\python.exe ingest_anythingllm_docs.py --config config.local.yaml --state sync.db --watch --interval 60
```

Both AnythingLLM and the sync process must keep running. Polling waits after
each cycle; uploads/OCR can take longer than the interval. It catches up after
restart. No startup task or service is installed automatically.

Keep the state database outside source roots. Back up an existing state database
before upgrading. Old rows are retained and migrated; because the original schema
did not store the target, keep the same server/workspace on the first upgraded run.

## Formats and PDF OCR

Supported: `txt md org adoc rst html docx xlsx pptx odt odp pdf mbox epub`.
Office text extraction still runs in AnythingLLM. Legacy `.doc/.xls/.ppt/.wps`,
spreadsheet recalculation and OCR of images inside Office documents are not
implemented by this tool.

PDF preprocessing is opt-in. Install `requirements-pdf.txt`, which includes
RapidOCR 3.9.2, CPU ONNX Runtime and the package's bundled Chinese/English models.
No separate OCR application or language-pack installation is needed. Models are
loaded from explicit local paths; missing models fail instead of downloading at
inference time. `pdf.model_dir` can select an existing local model directory.

- `pdf.mode: 'off'`: AnythingLLM's built-in PDF processing.
- `pdf.mode: auto`: retain sufficient page text; OCR each low-text page.
- `pdf.mode: force`: OCR every complete page, including text/image hybrid pages.

The original PDF stays read-only. Output contains filename and page numbers.
Only verified all-white pages without text are marked blank. An OCR failure or
empty result on a nonblank page fails the file, retaining the old indexed version.
OCR is approximate text extraction, not table, formula or diagram understanding.
See the Chinese guide for limitations and source references.

## Ownership and limitations

Deletion removes this tool's uploaded objects from the global AnythingLLM library
and every workspace reusing those objects. Prefer a dedicated sync workspace.
Do not manually reuse a synced object if it must outlive deletion of its source.

A lost upload response can leave an untracked server object because its IDs were
never received. The tool does not sweep unrelated workspace files to recover it.
This fork's tests do not replace acceptance against an installed Desktop version.

## Tests

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-pdf.txt
.\.venv\Scripts\python.exe -m pytest tests -q
```

Tests use real local files, SQLite and PDF parsing/rendering with simulated API
responses. Real OCR tests run with the optional PDF dependencies and a CJK font;
otherwise they skip. CI uses identical synthetic Chinese/English PDFs on Windows
and Linux, with network access blocked during OCR. Live AnythingLLM tests require explicit opt-in and a disposable
test workspace; see `tests/test_integration.py`.

MIT license, with the upstream copyright preserved.
