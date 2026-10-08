"""Transport contract tests use an explicit fake; they do not prove device readiness."""
import subprocess
import threading
import unittest
import tempfile
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient
from server import Device, DeviceError, create_app
from starlette.websockets import WebSocketDisconnect
from video import control_packet


class FakeDevice:
    def __init__(self):
        self.lock = threading.Lock()
        self.calls = []

    def status(self):
        return {"connected": False, "booted": False, "qidian_installed": False,
                "qidian_running": False, "error": "测试替身：无设备", "foreground": ""}

    def shell(self, *args, **kwargs):
        self.calls.append(args)
        return ""

    def connect(self):
        raise DeviceError("测试替身：设备不可连接")

    def screenshot(self):
        raise DeviceError("测试替身：无画面")


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.device = FakeDevice()
        self.client = TestClient(create_app(self.device))
        self.csrf = self.client.get("/api/debug/status").json()["csrf"]

    def post(self, path, body):
        return self.client.post("/api/debug/" + path, json=body, headers={"X-Debug-CSRF": self.csrf})

    def test_unconnected_device_is_not_ready_or_logged_in(self):
        state = self.client.get("/api/debug/status").json()
        self.assertFalse(state["device"]["connected"])
        self.assertFalse(state["login_verified"])
        self.assertEqual(self.post("connect", {}).status_code, 503)
        self.assertEqual(self.client.get("/api/debug/screenshot").status_code, 503)

    def test_cross_site_and_missing_csrf_are_rejected(self):
        self.assertEqual(self.client.post("/api/debug/tap", json={"x": 1, "y": 1}).status_code, 403)
        response = self.client.post("/api/debug/tap", json={"x": 1, "y": 1},
                                    headers={"X-Debug-CSRF": self.csrf, "Origin": "https://evil.invalid"})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.client.get("/", headers={"Host": "evil.invalid"}).status_code, 403)
        self.assertFalse(self.device.calls)

    def test_coordinates_and_key_allowlist(self):
        self.assertEqual(self.post("tap", {"x": -1, "y": 5}).status_code, 422)
        self.assertEqual(self.post("key/reboot", {}).status_code, 400)
        self.assertEqual(self.post("tap", {"x": 20, "y": 50}).status_code, 200)
        self.assertEqual(self.device.calls, [("input", "tap", "20", "50")])

    def test_text_is_not_returned_and_unsupported_text_fails(self):
        self.assertEqual(self.post("text", {"text": "中文"}).status_code, 400)
        self.assertEqual(self.post("text", {"text": "100%s"}).status_code, 400)
        response = self.post("text", {"text": "example 42"})
        self.assertEqual(response.json(), {"ok": True})
        self.assertEqual(self.device.calls, [("input", "text", "example%s42")])

    def test_command_quoting_and_timeout(self):
        device = Device()
        with patch("server.subprocess.run", return_value=subprocess.CompletedProcess([], 0, b"", b"")) as run:
            device.shell("input", "text", "x'; touch /bad; '")
            command = run.call_args.args[0]
            self.assertEqual(command[-2], "shell")
            import shlex
            self.assertEqual(shlex.split(command[-1]), ["input", "text", "x'; touch /bad; '"])
            self.assertNotIn("shell", run.call_args.kwargs)
        with patch("server.subprocess.run", side_effect=subprocess.TimeoutExpired("adb", 2)):
            with self.assertRaises(DeviceError):
                device.run("get-state")

    def test_invalid_screenshot_is_rejected(self):
        with patch.object(Device, "run", return_value=b"error: offline"):
            with self.assertRaises(DeviceError):
                Device().screenshot()


class RemoteTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.key = Path(self.directory.name) / 'test-token'
        self.token = 'test-only-' + 'x' * 40
        self.key.write_text(self.token)
        self.key.chmod(0o600)
        self.origin = 'https://site.example.invalid'
        self.device = FakeDevice()
        self.app = create_app(self.device, site_origin=self.origin,
                              api_origin='https://api.example.invalid', token_file=self.key)
        self.client = TestClient(self.app, base_url='https://api.example.invalid')
        self.headers = {'Origin': self.origin, 'Authorization': 'Bearer ' + self.token}

    def test_preflight_and_authenticated_status(self):
        response = self.client.options('/api/debug/status', headers={
            'Origin': self.origin, 'Access-Control-Request-Method': 'GET',
            'Access-Control-Request-Headers': 'authorization,x-debug-csrf'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers['access-control-allow-origin'], self.origin)
        self.assertNotIn('access-control-allow-credentials', response.headers)
        status = self.client.get('/api/debug/status', headers=self.headers)
        self.assertEqual(status.status_code, 200)
        self.assertEqual(status.headers['access-control-allow-origin'], self.origin)
        self.assertNotIn(self.token, status.text)

    def test_bad_origins_tokens_hosts_and_missing_csrf(self):
        bad = self.client.get('/api/debug/status', headers={'Origin': self.origin})
        self.assertEqual(bad.status_code, 401)
        self.assertEqual(bad.headers['access-control-allow-origin'], self.origin)
        headers = {**self.headers, 'Origin': 'https://evil.invalid'}
        self.assertEqual(self.client.get('/api/debug/status', headers=headers).status_code, 403)
        self.assertEqual(self.client.post('/api/debug/tap', json={'x': 1, 'y': 1}, headers=self.headers).status_code, 403)
        self.assertEqual(self.client.get('/api/debug/status', headers={**self.headers, 'Host': 'evil.invalid'}).status_code, 403)
        self.assertFalse(self.device.calls)

    def test_authenticated_control_keeps_csrf_check(self):
        csrf = self.client.get('/api/debug/status', headers=self.headers).json()['csrf']
        response = self.client.post('/api/debug/tap', json={'x': 12, 'y': 34},
                                    headers={**self.headers, 'X-Debug-CSRF': csrf})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.device.calls, [('input', 'tap', '12', '34')])

    def test_ws_bad_origin_and_invalid_first_auth_frame(self):
        with self.assertRaises(WebSocketDisconnect):
            with self.client.websocket_connect('/api/debug/stream', subprotocols=['qtrade-scrcpy-v1'],
                                                headers={'Host': '127.0.0.1', 'Origin': 'https://evil.invalid'}):
                self.fail('Unexpected WebSocket access')
        csrf = self.client.get('/api/debug/status', headers=self.headers).json()['csrf']
        with self.client.websocket_connect('/api/debug/stream', subprotocols=['qtrade-scrcpy-v1'],
                                            headers={'Host': '127.0.0.1', 'Origin': self.origin}) as ws:
            ws.send_json({'type': 'auth', 'token': 'wrong', 'csrf': csrf})
            with self.assertRaises(WebSocketDisconnect) as caught:
                ws.receive_json()
            self.assertEqual(caught.exception.code, 1008)

    def test_insecure_or_partial_remote_config_fails_closed(self):
        with self.assertRaises(ValueError):
            create_app(self.device, site_origin=self.origin)
        with self.assertRaises(ValueError):
            create_app(self.device, site_origin='http://public.example.invalid',
                       api_origin='https://api.example.invalid', token_file=self.key)
        self.key.chmod(0o644)
        with self.assertRaises(ValueError):
            create_app(self.device, site_origin=self.origin,
                       api_origin='https://api.example.invalid', token_file=self.key)

    def test_control_packets_reject_invalid_operations(self):
        import struct
        packet = control_packet({'type': 'touch', 'action': 'down', 'x': 20, 'y': 30}, 408, 720)
        self.assertEqual(struct.unpack('>BBQiiHHHII', packet), (2, 0, 0, 20, 30, 408, 720, 65535, 0, 0))
        select = control_packet({'type': 'key', 'key': 'select_all'}, 408, 720)
        self.assertEqual(list(struct.iter_unpack('>BBIII', select)),
                         [(0, 0, 29, 0, 0x1000), (0, 1, 29, 0, 0x1000)])
        for message in [{'type': 'touch', 'action': 'down', 'x': 408, 'y': 0},
                        {'type': 'key', 'key': 'reboot'}, {'type': 'shell', 'command': 'id'},
                        {'type': 'text', 'text': '\n'}]:
            with self.assertRaises(ValueError):
                control_packet(message, 408, 720)


if __name__ == "__main__":
    unittest.main(verbosity=2)
