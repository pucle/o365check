import os
import unittest

import _path  # noqa: F401
from server.db import _sql_statements, dsn_brief, normalize_dsn


class DsnTests(unittest.TestCase):
    def test_normalize_postgres_scheme(self):
        self.assertTrue(normalize_dsn("postgres://u:p@h/db").startswith("postgresql://"))

    def test_brief_hides_password(self):
        brief = dsn_brief("postgresql://user:super-secret@db.example.com:5432/club")
        self.assertNotIn("super-secret", brief)
        self.assertIn("db.example.com", brief)
        self.assertIn("club", brief)

    def test_sql_split_skips_comments(self):
        sql = "-- cmt\nCREATE TABLE a(id INT);\nCREATE INDEX i ON a(id);\n"
        stmts = _sql_statements(sql)
        self.assertEqual(len(stmts), 2)
        self.assertTrue(stmts[0].startswith("CREATE TABLE"))


@unittest.skipUnless(os.getenv("DATABASE_URL"), "cần DATABASE_URL (Neon/Postgres thử nghiệm)")
class PostgresStorageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from server.storage import Storage
        cls.store = Storage(os.environ["DATABASE_URL"])
        cls.mid = "pg_unit_test_member"

    @classmethod
    def tearDownClass(cls):
        try:
            cls.store.delete_member(cls.mid, keep_shifts=False)
        except Exception:
            pass
        cls.store.close()

    def test_ping_and_member_image_roundtrip(self):
        import hashlib
        self.store.ping()
        self.store.upsert_member(self.mid, "Unit", "2020-01-01T00:00:00")
        payload = b"\xff\xd8\xff" + b"jpeg-bytes-not-logged"
        digest = hashlib.sha256(payload).hexdigest()
        self.store.add_member_image(self.mid, "imgunit1", payload, "image/jpeg", "2020-01-01T00:00:00")
        got = self.store.get_member_image(self.mid, "imgunit1")
        self.assertIsNotNone(got)
        data, mime, n = got
        self.assertEqual(hashlib.sha256(data).hexdigest(), digest)
        self.assertEqual(mime, "image/jpeg")
        self.assertEqual(n, len(payload))
        n = self.store.delete_embeddings_by_img(self.mid, ["imgunit1"])
        self.assertGreaterEqual(n, 0)
        self.assertIsNone(self.store.get_member_image(self.mid, "imgunit1"))

    def test_reconnect_still_has_data(self):
        self.store.upsert_member(self.mid, "Unit", "2020-01-01T00:00:00")
        self.store.close()
        from server.storage import Storage
        self.__class__.store = Storage(os.environ["DATABASE_URL"])
        self.assertTrue(self.store.member_exists(self.mid))


if __name__ == "__main__":
    unittest.main()
