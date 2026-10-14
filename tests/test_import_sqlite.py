import os
import sqlite3
import tempfile
import unittest

import _path  # noqa: F401
from tools import import_sqlite as imp


class ImportSqliteLocalTests(unittest.TestCase):
    def test_counts_temp_db(self):
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        try:
            db = sqlite3.connect(path)
            db.executescript(
                "CREATE TABLE members(id TEXT PRIMARY KEY, name TEXT NOT NULL, created TEXT);"
                "INSERT INTO members VALUES('a','A','t');"
            )
            db.commit()
            db.close()
            c = imp.sqlite_counts(path)
            self.assertEqual(c["members"], 1)
            self.assertIsNone(c["embeddings"])
        finally:
            os.remove(path)

    def test_dsn_brief_not_in_counts(self):
        brief = __import__("server.db", fromlist=["dsn_brief"]).dsn_brief(
            "postgresql://u:secret@localhost/db"
        )
        self.assertNotIn("secret", brief)


if __name__ == "__main__":
    unittest.main()
