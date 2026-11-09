"""HTTP-level tests for the ReadAloudMaker UI server."""

import http.client
import json
import os
import shutil
import sys
import tempfile
import threading
import unittest

from readaloud_ui.server import App, make_server


def fake_detector(force=False):
    return {
        "repo": os.getcwd(),
        "platform": "test",
        "runningPython": "3.9",
        "ffmpeg": {"ok": True, "ffmpeg": "ffmpeg", "ffprobe": "ffprobe", "espeak": ""},
        "gpu": "NVIDIA GeForce GTX 1050 Ti",
        "gpus": [
            {"index": 0, "name": "NVIDIA GeForce GTX 1050 Ti",
             "totalMemory": 4096 * 1024 * 1024, "freeMemory": 3100 * 1024 * 1024,
             "computeCap": "6.1", "driver": "582.16"},
            {"index": 1, "name": "NVIDIA GeForce GTX 1060 6GB",
             "totalMemory": 6144 * 1024 * 1024, "freeMemory": 5700 * 1024 * 1024,
             "computeCap": "6.1", "driver": "582.16"},
        ],
        "recommendedGpu": 1,
        "engines": [
            {
                "key": "qwen",
                "name": "Qwen3 Forced Aligner",
                "subtitle": "Word-level timing",
                "available": True,
                "command": [sys.executable],
                "pythonLabel": "test python",
                "missing": [],
                "notes": ["~32-43 ms"],
                "defaults": {},
            },
            {
                "key": "classic",
                "name": "Classic",
                "subtitle": "aeneas + syncabook",
                "available": False,
                "command": [],
                "pythonLabel": "not found",
                "missing": ["aeneas"],
                "notes": [],
                "defaults": {},
            },
        ],
        "candidates": [],
    }


class UiServerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.mkdtemp(prefix="readaloud-ui-test-")
        cls.upload = os.path.join(cls.temp, "uploads")
        os.makedirs(cls.upload)
        cls.app = App(detector=fake_detector, upload_dir=cls.upload)
        cls.server = make_server("127.0.0.1", 0, cls.app)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)
        shutil.rmtree(cls.temp, ignore_errors=True)

    def request(self, method, path, body=None, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=20)
        payload = None
        request_headers = dict(headers or {})
        if isinstance(body, (dict, list)):
            payload = json.dumps(body).encode("utf-8")
            request_headers.setdefault("Content-Type", "application/json")
        elif isinstance(body, str):
            payload = body.encode("utf-8")
        else:
            payload = body
        connection.request(method, path, body=payload, headers=request_headers)
        response = connection.getresponse()
        data = response.read()
        content_type = response.getheader("Content-Type") or ""
        status = response.status
        connection.close()
        return status, data, content_type

    def json_request(self, method, path, body=None, headers=None):
        status, data, _ = self.request(method, path, body, headers)
        try:
            return status, json.loads(data.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return status, None

    # -- static ------------------------------------------------------------
    def test_index_is_served(self):
        status, data, content_type = self.request("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn("text/html", content_type)
        self.assertIn(b"ReadAloudMaker", data)
        self.assertIn(b'id="buildForm"', data)

    def test_css_and_js_are_served(self):
        for path in ("/assets/app.css", "/assets/app.js", "/assets/favicon.svg"):
            status, data, _ = self.request("GET", path)
            self.assertEqual(status, 200, path)
            self.assertGreater(len(data), 100)

    def test_asset_traversal_is_rejected(self):
        status, _, _ = self.request("GET", "/assets/../server.py")
        self.assertEqual(status, 404)

    # -- environment -------------------------------------------------------
    def test_env_endpoint(self):
        status, payload = self.json_request("GET", "/api/env")
        self.assertEqual(status, 200)
        self.assertEqual(payload["app"], "ReadAloudMaker")
        self.assertEqual(payload["port"], self.port)
        keys = [engine["key"] for engine in payload["engines"]]
        self.assertEqual(keys, ["qwen", "classic"])

    def test_env_lists_every_gpu(self):
        status, payload = self.json_request("GET", "/api/env")
        self.assertEqual(status, 200)
        self.assertEqual([gpu["index"] for gpu in payload["gpus"]], [0, 1])
        self.assertEqual(payload["recommendedGpu"], 1)

    def test_ui_has_no_hardcoded_device_list(self):
        status, data, _ = self.request("GET", "/assets/app.js")
        self.assertEqual(status, 200)
        self.assertIn(b"renderDevices", data)
        status, data, _ = self.request("GET", "/")
        self.assertNotIn(b'value="cuda:1"', data)

    def test_non_local_host_is_rejected(self):
        status, _, _ = self.request("GET", "/api/env", headers={"Host": "evil.example"})
        self.assertEqual(status, 403)

    def test_cross_origin_post_is_rejected(self):
        status, _ = self.json_request(
            "POST", "/api/scan",
            body={"folder": self.temp},
            headers={"Origin": "https://evil.example"},
        )
        self.assertEqual(status, 403)

    # -- scan --------------------------------------------------------------
    def test_scan_requires_a_folder(self):
        status, payload = self.json_request("POST", "/api/scan", body={"folder": ""})
        self.assertEqual(status, 400)
        self.assertIn("error", payload)

    def test_scan_lists_files(self):
        folder = tempfile.mkdtemp(dir=self.temp)
        with open(os.path.join(folder, "novel.epub"), "wb") as handle:
            handle.write(b"not really an epub")
        with open(os.path.join(folder, "chapter1.mp3"), "wb") as handle:
            handle.write(b"audio")
        status, payload = self.json_request(
            "POST", "/api/scan", body={"folder": folder, "engine": "qwen"})
        self.assertEqual(status, 200)
        self.assertEqual(payload["folder"], os.path.abspath(folder))
        self.assertEqual([item["name"] for item in payload["audio"]], ["chapter1.mp3"])
        self.assertIn("epub", payload)
        # A broken EPUB must not crash the scan; it becomes a warning.
        self.assertTrue(payload["warnings"])

    # -- uploads -----------------------------------------------------------
    def test_upload_and_reset(self):
        status, payload = self.json_request(
            "POST", "/api/upload?name=chapter1.mp3", body=b"fake audio bytes")
        self.assertEqual(status, 201)
        self.assertEqual(payload["name"], "chapter1.mp3")
        self.assertTrue(os.path.isfile(payload["path"]))
        with open(payload["path"], "rb") as handle:
            self.assertEqual(handle.read(), b"fake audio bytes")

        status, payload = self.json_request("POST", "/api/upload/reset", body={})
        self.assertEqual(status, 200)
        self.assertEqual(payload["removed"], 1)
        self.assertEqual(os.listdir(self.upload), [])

    def test_upload_rejects_other_extensions(self):
        status, payload = self.json_request(
            "POST", "/api/upload?name=payload.exe", body=b"nope")
        self.assertEqual(status, 400)
        self.assertIn("Unsupported", payload["error"])

    # -- jobs --------------------------------------------------------------
    def test_job_with_missing_folder_is_rejected(self):
        status, payload = self.json_request(
            "POST", "/api/jobs",
            body={"engine": "qwen", "folder": os.path.join(self.temp, "missing")},
        )
        self.assertEqual(status, 400)
        self.assertIn("Folder not found", payload["error"])

    def test_unavailable_engine_is_rejected(self):
        status, payload = self.json_request(
            "POST", "/api/jobs",
            body={"engine": "classic", "folder": self.temp},
        )
        self.assertEqual(status, 400)
        self.assertIn("not ready", payload["error"])
        self.assertIn("aeneas", payload["hint"])

    def test_unknown_job_is_404(self):
        status, _ = self.json_request("GET", "/api/jobs/deadbeef")
        self.assertEqual(status, 404)
        status, _ = self.json_request("POST", "/api/jobs/deadbeef/cancel", body={})
        self.assertEqual(status, 404)
        status, _ = self.json_request("GET", "/api/jobs/deadbeef/download")
        self.assertEqual(status, 404)

    def test_unknown_route_is_404(self):
        status, _ = self.json_request("GET", "/api/nope")
        self.assertEqual(status, 404)


if __name__ == "__main__":
    unittest.main()
