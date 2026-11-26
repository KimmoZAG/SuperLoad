import hashlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from zipfile import ZipFile

from report.release_data import ResultsRelease


class ResultsReleaseTest(unittest.TestCase):
    def test_downloads_verified_archive_and_reuses_it(self):
        with tempfile.TemporaryDirectory() as directory:
            cache = Path(directory)
            payload = io.BytesIO()
            with ZipFile(payload, "w") as archive:
                archive.writestr("buffered/example.json", '{"status":"ok"}')
            package = payload.getvalue()
            release = ResultsRelease("results-test", "data.zip", hashlib.sha256(package).hexdigest())
            with patch("report.release_data.urlopen", return_value=io.BytesIO(package)) as download:
                for _ in range(2):
                    with release.unpack(cache) as out:
                        self.assertEqual((out / "buffered/example.json").read_text(), '{"status":"ok"}')
                download.assert_called_once()

    def test_rejects_bad_checksum_before_extraction(self):
        with tempfile.TemporaryDirectory() as directory:
            cache = Path(directory)
            release = ResultsRelease("results-test", "data.zip", "0" * 64)
            with patch("report.release_data.urlopen", return_value=io.BytesIO(b"invalid content")):
                with self.assertRaisesRegex(ValueError, "SHA-256 mismatch"), release.unpack(cache):
                    self.fail("must reject an unverified archive")
            self.assertEqual(list(cache.iterdir()), [])
