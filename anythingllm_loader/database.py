"""SQLite ownership and retry journal. Existing document rows are preserved."""
from datetime import datetime
from pathlib import Path
import os
import sqlite3

CONFIG_DIR = Path.home() / ".anythingllm-sync"
DATABASE_FILENAME = CONFIG_DIR / "uploaded-docs.db"


def document_path_key(path):
    """Match Windows paths independently of spelling/case, keeping display paths."""
    return os.path.normcase(os.path.abspath(os.path.expanduser(str(path))))


class AnythingLLMDocument:
    def __init__(self, local_file_path, upload_timestamp, anythingllm_document_location,
                 content, content_hash=None, generation="legacy", status="active",
                 target_key=None, mtime_ns=None, size=None, row_id=None, path_key=None):
        self.local_file_path = local_file_path
        self.upload_timestamp = upload_timestamp
        self.anythingllm_document_location = anythingllm_document_location
        self.content = content
        self.content_hash = content_hash
        self.generation = generation
        self.status = status
        self.target_key = target_key
        self.mtime_ns = mtime_ns
        self.size = size
        self.row_id = row_id
        self.path_key = path_key or document_path_key(local_file_path)


class DocumentDatabase:
    def __init__(self, state_path=None):
        self.path = Path(state_path) if state_path else Path(DATABASE_FILENAME)

    def get_connection(self):
        return sqlite3.connect(self.path, timeout=30)

    def initialize_database(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.get_connection() as conn:
            conn.execute("""CREATE TABLE IF NOT EXISTS documents (
                id INTEGER PRIMARY KEY, local_file_path TEXT, upload_timestamp DATETIME,
                anythingllm_document_location TEXT, content TEXT)""")
            columns = {row[1] for row in conn.execute("PRAGMA table_info(documents)")}
            for name, definition in {
                "content_hash": "TEXT", "generation": "TEXT DEFAULT 'legacy'",
                "status": "TEXT DEFAULT 'active'", "target_key": "TEXT",
                "mtime_ns": "INTEGER", "size": "INTEGER",
                "path_key": "TEXT",
            }.items():
                if name not in columns:
                    conn.execute(f"ALTER TABLE documents ADD COLUMN {name} {definition}")
            # Legacy paths remain readable; comparisons use the host's path identity.
            for row_id, path in conn.execute("SELECT id, local_file_path FROM documents WHERE path_key IS NULL").fetchall():
                conn.execute("UPDATE documents SET path_key = ? WHERE id = ?", (document_path_key(path), row_id))
        return True

    def add_document(self, doc):
        self.add_documents([doc])

    def add_documents(self, documents):
        # Every returned worksheet is journalled in one transaction before embedding.
        with self.get_connection() as conn:
            conn.executemany("""INSERT INTO documents (
                local_file_path, upload_timestamp, anythingllm_document_location, content,
                content_hash, generation, status, target_key, mtime_ns, size, path_key)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""", [(
                    d.local_file_path, d.upload_timestamp.isoformat(),
                    d.anythingllm_document_location, d.content, d.content_hash,
                    d.generation, d.status, d.target_key, d.mtime_ns, d.size, d.path_key,
                ) for d in documents])

    def activate_generation(self, path, generation, target_key):
        with self.get_connection() as conn:
            conn.execute("UPDATE documents SET status = CASE WHEN generation = ? THEN 'active' ELSE 'retired' END "
                         "WHERE path_key = ? AND target_key = ?",
                         (generation, document_path_key(path), target_key))

    def bind_legacy_documents(self, target_key):
        with self.get_connection() as conn:
            conn.execute("UPDATE documents SET target_key = ? WHERE target_key IS NULL", (target_key,))

    def invalidate_generation(self, generation, target_key):
        with self.get_connection() as conn:
            conn.execute("UPDATE documents SET content_hash = NULL WHERE generation = ? AND target_key = ?",
                         (generation, target_key))

    def remove_record(self, row_id):
        with self.get_connection() as conn:
            conn.execute("DELETE FROM documents WHERE id = ?", (row_id,))

    def remove_document(self, local_document_path):
        with self.get_connection() as conn:
            conn.execute("DELETE FROM documents WHERE path_key = ?", (document_path_key(local_document_path),))

    def get_documents(self, target_key=None):
        with self.get_connection() as conn:
            query = ("SELECT local_file_path, upload_timestamp, anythingllm_document_location, content, "
                     "content_hash, generation, status, target_key, mtime_ns, size, id, path_key FROM documents")
            rows = conn.execute(query + (" WHERE target_key = ?" if target_key else ""),
                                (target_key,) if target_key else ()).fetchall()
        return [AnythingLLMDocument(row[0], datetime.fromisoformat(row[1]), *row[2:]) for row in rows]
