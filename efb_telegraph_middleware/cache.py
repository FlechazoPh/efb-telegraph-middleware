"""SQLite cache: archived pages by URL and re-hosted images by content hash."""

import sqlite3
import threading
import time
from pathlib import Path
from typing import Optional


class Cache:
    def __init__(self, path: Path, page_ttl_days: float = 30):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.page_ttl = page_ttl_days * 86400
        self.lock = threading.Lock()
        self.db = sqlite3.connect(str(self.path), check_same_thread=False)
        with self.lock, self.db:
            self.db.execute("CREATE TABLE IF NOT EXISTS pages (url TEXT PRIMARY KEY, page_url TEXT,"
                            " title TEXT, created INTEGER)")
            self.db.execute("CREATE TABLE IF NOT EXISTS images (sha TEXT PRIMARY KEY, url TEXT,"
                            " created INTEGER)")

    def get_page(self, url: str) -> Optional[str]:
        if self.page_ttl <= 0:
            return None
        with self.lock:
            row = self.db.execute("SELECT page_url, created FROM pages WHERE url = ?", (url,)).fetchone()
        if row and time.time() - row[1] < self.page_ttl:
            return row[0]
        return None

    def put_page(self, url: str, page_url: str, title: str) -> None:
        with self.lock, self.db:
            self.db.execute("INSERT OR REPLACE INTO pages VALUES (?, ?, ?, ?)",
                            (url, page_url, title, int(time.time())))

    def get_image(self, sha: str) -> Optional[str]:
        with self.lock:
            row = self.db.execute("SELECT url FROM images WHERE sha = ?", (sha,)).fetchone()
        return row[0] if row else None

    def put_image(self, sha: str, url: str) -> None:
        with self.lock, self.db:
            self.db.execute("INSERT OR REPLACE INTO images VALUES (?, ?, ?)", (sha, url, int(time.time())))
