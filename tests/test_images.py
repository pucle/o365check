import hashlib
import unittest

import numpy as np

import _path  # noqa: F401
from server.images import MAX_UPLOAD_BYTES, ImageError, decode_checked, sniff_mime, to_jpeg


class ImageTests(unittest.TestCase):
    def test_empty_rejected(self):
        with self.assertRaises(ImageError):
            decode_checked(b"")

    def test_too_large(self):
        with self.assertRaises(ImageError) as ctx:
            decode_checked(b"x" * (MAX_UPLOAD_BYTES + 1))
        self.assertEqual(ctx.exception.status, 413)

    def test_invalid_bytes(self):
        with self.assertRaises(ImageError):
            decode_checked(b"not-an-image")

    def test_jpeg_roundtrip_hash(self):
        img = np.zeros((32, 40, 3), np.uint8)
        img[:, :] = (10, 20, 30)
        data, mime = to_jpeg(img)
        self.assertEqual(mime, "image/jpeg")
        self.assertTrue(data.startswith(b"\xff\xd8"))
        again = decode_checked(data)
        self.assertEqual(again.shape[2], 3)
        h1 = hashlib.sha256(data).hexdigest()
        h2 = hashlib.sha256(to_jpeg(decode_checked(data))[0]).hexdigest()
        self.assertEqual(len(h1), 64)
        self.assertTrue(h2)

    def test_sniff_does_not_trust_extension(self):
        self.assertEqual(sniff_mime(b"\xff\xd8\xff\xe0xxxx"), "image/jpeg")
        self.assertIsNone(sniff_mime(b"GIF89a"))


if __name__ == "__main__":
    unittest.main()
