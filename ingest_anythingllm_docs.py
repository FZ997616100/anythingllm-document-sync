"""Recursive, journalled sync; run once by default, or poll with --watch."""
import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import time
import uuid

from anythingllm_loader.database import DocumentDatabase, AnythingLLMDocument, document_path_key
from anythingllm_loader.anythingllm_api import AnythingLLM, APIError
from anythingllm_loader.config import AnythingLLMConfig


def _excluded(path, config):
    return (any(os.path.normcase(value) in os.path.normcase(path.name) for value in config.file_excludes)
            or any(os.path.normcase(value) in document_path_key(path) for value in config.directory_excludes))


def _managed(path, config):
    path = Path(path).absolute()
    return (path.suffix.lower().lstrip('.') in AnythingLLM.supported_file_types()
            and not _excluded(path, config)
            and any(path == Path(root).absolute() or Path(root).absolute() in path.parents
                    for root in config.file_paths))


def scan_local_documents(config):
    documents, errors = {}, []
    for configured in config.file_paths:
        root = Path(configured).absolute()
        try:
            if not root.is_dir():
                raise OSError(f"Source directory unavailable: {root}")
            def onerror(error):
                raise error
            for directory, directories, files in os.walk(root, onerror=onerror, followlinks=False):
                directories[:] = [name for name in directories
                                  if not any(os.path.normcase(value) in document_path_key(Path(directory) / name)
                                             for value in config.directory_excludes)
                                  and not (Path(directory) / name).is_symlink()]
                for filename in files:
                    path = Path(directory) / filename
                    if not path.is_symlink() and _managed(path, config):
                        # Explicit stat surfaces unreadable/disappearing entries; no deletion on partial scan.
                        path.stat()
                        documents.setdefault(document_path_key(path), str(path))
        except OSError as exc:
            errors.append(str(exc))
    return sorted(documents.values()), errors


def fetch_local_documents(config):
    documents, errors = scan_local_documents(config)
    if errors:
        raise OSError("; ".join(errors))
    return documents


def _fingerprint(path, pdf_options=None):
    digest = hashlib.sha256()
    if Path(path).suffix.lower() == ".pdf":
        options = {"mode": "off", **(pdf_options or {})}
        if options["mode"] != "off":
            options = {"min_text_chars": 30, "dpi": 300, "model_dir": None, **options}
            # Engine changes must also replace previously extracted PDF text.
            digest.update(b"rapidocr-3.9.2\x00")
        else:
            options = {"mode": "off"}
        # Changing extraction settings changes the processed content even with identical source bytes.
        digest.update(json.dumps(options, sort_keys=True, ensure_ascii=False).encode("utf-8"))
        digest.update(b"\x00")
    with open(path, "rb") as source:
        before = os.fstat(source.fileno())
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
        after = os.fstat(source.fileno())
    if (before.st_mtime_ns, before.st_size) != (after.st_mtime_ns, after.st_size):
        raise OSError("File changed while hashing")
    return digest.hexdigest(), after.st_mtime_ns, after.st_size


def target_key(config):
    return json.dumps([config.anythingllm_url.rstrip('/'), config.workspace_slug])


def _save_upload(database, path, documents, fingerprint, generation, target, complete=True):
    digest, mtime_ns, size = fingerprint
    # Store ownership/metadata, not a second copy of extracted source content.
    database.add_documents([AnythingLLMDocument(
        path, datetime.now(timezone.utc), doc["location"],
        json.dumps({key: value for key, value in doc.items() if key != "pageContent"}),
        digest if complete else None, generation, "pending", target, mtime_ns, size,
    ) for doc in documents])


def _ensure_embedded(api, documents):
    embedded = set(api.fetch_embedded_workspace_documents())
    for doc in documents:
        if doc.anythingllm_document_location not in embedded:
            if not api.embed_new_document(doc.anythingllm_document_location):
                raise APIError("Embedding was not confirmed")
    # Recheck the complete set before retiring even one old object.
    expected = {doc.anythingllm_document_location for doc in documents}
    if not expected.issubset(api.fetch_embedded_workspace_documents()):
        raise APIError("Not all uploaded objects are workspace members")


def _sync_file(api, database, path, target, pdf_options=None):
    fingerprint = _fingerprint(path, pdf_options)
    path_key = document_path_key(path)
    rows = [d for d in database.get_documents(target) if d.path_key == path_key]
    matching = [d for d in rows if d.content_hash == fingerprint[0] and d.status != "retired"]
    if matching:
        # A partially embedded generation is retried without another upload.
        generation = matching[-1].generation
        documents = [d for d in matching if d.generation == generation]
        try:
            _ensure_embedded(api, documents)
            database.activate_generation(path, generation, target)
            return
        except APIError:
            if any(d.status == "pending" for d in documents):
                raise
            # An externally removed active object can be replaced by a fresh upload.
    generation = uuid.uuid4().hex
    try:
        response = api.upload_document(path)
    except APIError as exc:
        if exc.documents:
            _save_upload(database, path, exc.documents, fingerprint, generation, target, complete=False)
        raise
    _save_upload(database, path, response, fingerprint, generation, target)
    # A concurrent edit must not promote an upload labelled with the wrong file hash.
    try:
        unchanged = _fingerprint(path, pdf_options) == fingerprint
    except OSError:
        database.invalidate_generation(generation, target)
        raise
    if not unchanged:
        database.invalidate_generation(generation, target)
        raise OSError("File changed during upload; recorded objects will be retried/replaced")
    documents = [d for d in database.get_documents(target) if d.generation == generation]
    _ensure_embedded(api, documents)
    database.activate_generation(path, generation, target)


def _delete_owned(api, database, doc):
    location = doc.anythingllm_document_location
    if location in api.fetch_embedded_workspace_documents():
        if not api.unembed_document(location):
            raise APIError("Unembedding was not confirmed")
    if not api.unload_document(location):
        raise APIError("Removal was not confirmed")
    database.remove_record(doc.row_id)


def sync_once(config, database, api=None):
    api = api or AnythingLLM(config)
    if not api.authenticate():
        raise APIError("Authentication failed")
    # Validate the destination before uploading anything to the global store.
    api.fetch_embedded_workspace_documents()
    target = target_key(config)
    database.bind_legacy_documents(target)
    local_documents, scan_errors = scan_local_documents(config)
    errors = list(scan_errors)
    for path in local_documents:
        try:
            _sync_file(api, database, path, target, getattr(config, "pdf", {}))
        except (APIError, OSError, ValueError) as exc:
            errors.append(f"{path}: {exc}")
    local_set = {document_path_key(path) for path in local_documents}
    # A missing/unreadable root never becomes a mass-delete signal.
    if not scan_errors:
        for doc in database.get_documents(target):
            if not _managed(doc.local_file_path, config):
                continue
            if doc.status == "retired" or doc.path_key not in local_set:
                try:
                    _delete_owned(api, database, doc)
                except (APIError, OSError, ValueError) as exc:
                    errors.append(f"Cleanup {doc.local_file_path}: {exc}")
    for error in errors:
        print(f"Sync incomplete: {error}")
    print(f"Checked {len(local_documents)} local files; {len(errors)} error(s).")
    return not errors


@contextmanager
def state_lock(state_path):
    """OS-owned lock releases on exit/crash; no stale PID lock recovery is needed."""
    lock_path = Path(str(state_path) + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+b") as handle:
        handle.seek(0)
        if lock_path.stat().st_size == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise RuntimeError("Another sync process is using this state database") from exc
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == "nt":
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle, fcntl.LOCK_UN)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--watch", action="store_true", help="Poll until stopped")
    parser.add_argument("--interval", type=float, default=60, help="Polling seconds (default: 60)")
    parser.add_argument("--config", type=Path, help="YAML configuration path")
    parser.add_argument("--state", type=Path, help="SQLite ownership/retry state path")
    args = parser.parse_args(argv)
    if args.interval <= 0:
        parser.error("--interval must be positive")
    database = DocumentDatabase(args.state)
    try:
        with state_lock(database.path):
            database.initialize_database()
            while True:
                try:
                    config = AnythingLLMConfig.load_config(args.config)
                    success = sync_once(config, database)
                except (APIError, OSError, ValueError, KeyError) as exc:
                    print(f"Sync incomplete: {exc}")
                    success = False
                if not args.watch:
                    return 0 if success else 1
                time.sleep(args.interval)
    except KeyboardInterrupt:
        return 0
    except RuntimeError as exc:
        print(str(exc))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
