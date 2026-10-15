import asyncio
import base64
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx
from fastapi.testclient import TestClient

import api


PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+a0X8AAAAASUVORK5CYII=")
FIXTURE_CLI = '''import base64, json, os, sys, time
from pathlib import Path
args = sys.argv[1:]
output = Path(args[args.index('-o') + 1])
references = [args[i + 1] for i, arg in enumerate(args[:-1]) if arg == '--ref']
Path(os.environ['IMAGEGEN_TEST_RECORD']).write_text(json.dumps({
    'args': args, 'output': str(output),
    'references': [base64.b64encode(Path(path).read_bytes()).decode() for path in references],
}))
mode = os.environ.get('IMAGEGEN_TEST_MODE', 'ok')
if mode == 'slow': time.sleep(0.5)
if mode in ('auth', 'quota', 'error'):
    print({'auth': 'token refresh failed: HTTP 401 private-marker', 'quota': 'HTTP 429 private-marker', 'error': 'private-marker'}[mode], file=sys.stderr)
    sys.exit(1)
if mode in ('progress-error', 'usage-error', 'timeout', 'stalled', 'model'):
    print({
        'progress-error': '[   0.0s] requesting (backend=codex format=png timeout=420s) private-marker\\nHTTP 400: unsupported model',
        'usage-error': 'usage: chatgpt-imagegen [--timeout TIMEOUT] [--stall-timeout STALL_TIMEOUT]\\nerror: unrecognized arguments: private-marker',
        'timeout': 'stream exceeded total timeout budget; aborting private-marker',
        'stalled': 'stalled: the image backend sent no data for ~120s private-marker',
        'model': '[   0.0s] requesting (backend=codex timeout=420s): "quota private-marker"\\nHTTP 400: {"detail":"The gpt-5.5 model is not supported when using Codex with a ChatGPT account."}',
    }[mode], file=sys.stderr)
    sys.exit(1)
if mode != 'missing':
    output.write_bytes(b'not an image' if mode == 'invalid' else base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+a0X8AAAAASUVORK5CYII='))
'''


class APIFixture:
    def setUp(self):
        super().setUp()
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.cli = self.root / "fixture_cli.py"
        self.cli.write_text(FIXTURE_CLI)
        self.record = self.root / "record.json"
        environment = patch.dict(os.environ, {
            "IMAGEGEN_API_TOKEN": "test-api-token",
            "IMAGEGEN_CLI_PATH": str(self.cli),
            "CHATGPT_IMAGEGEN_BACKEND": "web",
            "IMAGEGEN_TEST_RECORD": str(self.record),
            "IMAGEGEN_TEST_MODE": "ok",
        })
        environment.start()
        self.addCleanup(environment.stop)
        self.headers = {"Authorization": "Bearer test-api-token"}


class GenerateAPI(APIFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.client = TestClient(api.app)

    def generate(self, prompt="A printed poster", **kwargs):
        return self.client.post("/generate", data={"prompt": prompt}, headers=self.headers, **kwargs)

    def test_subprocess_returns_image_and_forwards_references(self):
        response = self.generate("--help", files=[("references", ("../../ref.png", PNG, "image/png"))])
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.headers["content-type"], "image/png")
        self.assertEqual(response.content, PNG)
        record = json.loads(self.record.read_text())
        self.assertEqual(record["args"][-2:], ["--", "--help"])
        self.assertEqual(record["args"][record["args"].index("--backend") + 1], "web")
        self.assertEqual(record["args"][record["args"].index("--stall-timeout") + 1], record["args"][record["args"].index("--timeout") + 1])
        self.assertEqual(record["references"], [base64.b64encode(PNG).decode()])
        self.assertFalse(Path(record["output"]).parent.exists(), "temporary images were not removed")

    def test_rejects_wrong_api_token_without_running_cli(self):
        response = self.client.post("/generate", data={"prompt": "poster"}, headers={"Authorization": "Bearer incorrect"})
        self.assertEqual(response.status_code, 401)
        self.assertFalse(self.record.exists())

    def test_reports_missing_api_configuration(self):
        with patch.dict(os.environ, {"IMAGEGEN_API_TOKEN": ""}):
            response = self.generate()
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["detail"]["code"], "imagegen_not_configured")

    def test_auth_and_quota_errors_are_distinct_and_do_not_leak_output(self):
        for mode, status, code in [("auth", 503, "imagegen_auth_required"), ("quota", 429, "imagegen_rate_limited"), ("error", 502, "imagegen_failed")]:
            with self.subTest(mode=mode), patch.dict(os.environ, {"IMAGEGEN_TEST_MODE": mode}):
                response = self.generate()
                self.assertEqual(response.status_code, status)
                self.assertEqual(response.json()["detail"]["code"], code)
                self.assertNotIn("private-marker", response.text)

    def test_timeout_kills_generation_and_returns_gateway_timeout(self):
        with patch.dict(os.environ, {"IMAGEGEN_TEST_MODE": "slow"}), patch.object(api, "PROCESS_TIMEOUT", 0.1):
            response = self.generate()
        self.assertEqual(response.status_code, 504)
        self.assertEqual(response.json()["detail"]["code"], "imagegen_timeout")
        if self.record.exists():
            self.assertFalse(Path(json.loads(self.record.read_text())["output"]).parent.exists())

    def test_progress_and_usage_text_do_not_mask_real_failures_as_timeouts(self):
        for mode, status, code in [
            ("progress-error", 502, "imagegen_failed"),
            ("usage-error", 502, "imagegen_failed"),
            ("timeout", 504, "imagegen_timeout"),
            ("stalled", 504, "imagegen_stalled"),
            ("model", 503, "imagegen_model_unsupported"),
        ]:
            with self.subTest(mode=mode), patch.dict(os.environ, {"IMAGEGEN_TEST_MODE": mode}):
                with self.assertLogs(api.logger, level="WARNING") as captured:
                    response = self.generate()
                self.assertEqual(response.status_code, status)
                self.assertEqual(response.json()["detail"]["code"], code)
                self.assertNotIn("private-marker", response.text)
                self.assertIn(code, captured.output[0])
                self.assertIn("elapsed_seconds=", captured.output[0])
                self.assertNotIn("private-marker", "".join(captured.output))

    def test_invalid_and_missing_output_are_rejected(self):
        for mode in ["invalid", "missing"]:
            with self.subTest(mode=mode), patch.dict(os.environ, {"IMAGEGEN_TEST_MODE": mode}):
                response = self.generate()
                self.assertEqual(response.status_code, 502)
                self.assertEqual(response.json()["detail"]["code"], "imagegen_invalid_image")

    def test_invalid_references_and_prompt_are_rejected_before_generation(self):
        for contents in [b"", b"not an image", PNG + b"x" * api.MAX_FILE_SIZE]:
            with self.subTest(length=len(contents)):
                response = self.generate(files=[("references", ("ref.png", contents, "image/png"))])
                self.assertEqual(response.status_code, 400)
        for prompt in [" ", "x" * 8001]:
            self.assertEqual(self.generate(prompt).status_code, 400)
        self.assertFalse(self.record.exists())

    def test_default_cli_is_resolved_from_repository(self):
        with patch.dict(os.environ, {"IMAGEGEN_CLI_PATH": ""}), patch.object(api.subprocess, "run", side_effect=OSError("test start failure")) as run:
            response = self.generate()
        self.assertEqual(response.status_code, 503)
        self.assertEqual(Path(run.call_args.args[0][1]), Path(api.__file__).with_name("chatgpt-imagegen"))


class ConcurrentAPI(APIFixture, unittest.IsolatedAsyncioTestCase):
    async def test_health_responds_while_cli_is_generating(self):
        transport = httpx.ASGITransport(app=api.app)
        with patch.dict(os.environ, {"IMAGEGEN_TEST_MODE": "slow"}):
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                generation = asyncio.create_task(client.post("/generate", data={"prompt": "poster"}, headers=self.headers))
                try:
                    for _ in range(200):
                        if self.record.exists():
                            break
                        await asyncio.sleep(0.01)
                    self.assertTrue(self.record.exists(), "CLI did not start")
                    self.assertFalse(generation.done(), "generation blocked the event loop")
                    health = await asyncio.wait_for(client.get("/health"), timeout=0.2)
                    self.assertEqual(health.status_code, 200)
                finally:
                    response = await generation
                self.assertEqual(response.status_code, 200)


if __name__ == "__main__":
    unittest.main()
