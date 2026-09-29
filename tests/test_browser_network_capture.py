import base64
import gzip
import json
from pathlib import Path
import unittest

from browser_network_capture import BrowserNetworkCaptureDriver


def performance_entry(method, params):
    return {
        "message": json.dumps(
            {"message": {"method": method, "params": params}, "webview": "test"}
        )
    }


def request_events(request_id, url, *, post_data="", headers=None, response_headers=None, body=None):
    events = [
        performance_entry(
            "Network.requestWillBeSent",
            {
                "requestId": request_id,
                "request": {
                    "url": url,
                    "method": "POST" if post_data else "GET",
                    "headers": headers or {"Accept": "application/json"},
                    "postData": post_data,
                },
            },
        ),
        performance_entry(
            "Network.responseReceived",
            {
                "requestId": request_id,
                "response": {
                    "status": 200,
                    "headers": response_headers or {"Content-Type": "application/json"},
                },
            },
        ),
    ]
    if body is not None:
        events.append(performance_entry("Network.loadingFinished", {"requestId": request_id}))
    return events


class FakeDriver:
    def __init__(self):
        self.logs = []
        self.commands = []
        self.response_bodies = {}

    def add_events(self, events):
        self.logs.extend(events)

    def get_log(self, log_type):
        if log_type != "performance":
            raise AssertionError(f"unexpected log type: {log_type}")
        logs, self.logs = self.logs, []
        return logs

    def execute_cdp_cmd(self, command, args):
        self.commands.append((command, args))
        if command == "Network.enable":
            return {}
        if command == "Network.getRequestPostData":
            return {"postData": ""}
        if command == "Network.getResponseBody":
            return self.response_bodies[args["requestId"]]
        raise AssertionError(f"unexpected command: {command}")

    @property
    def title(self):
        return "delegated property"


class BrowserNetworkCaptureTests(unittest.TestCase):
    def setUp(self):
        self.raw_driver = FakeDriver()
        self.driver = BrowserNetworkCaptureDriver(self.raw_driver)

    def test_wait_returns_compatible_request_and_gzip_response(self):
        payload = b'{"flightSegments":[{"departureCityName":"Shanghai"}]}'
        compressed = gzip.compress(payload)
        self.raw_driver.response_bodies["batch"] = {
            "base64Encoded": True,
            "body": base64.b64encode(compressed).decode("ascii"),
        }
        self.raw_driver.add_events(
            request_events(
                "batch",
                "https://flights.test/international/search/api/search/batchSearch?x=1",
                post_data='{"search":true}',
                response_headers={
                    "Content-Type": "application/json",
                    "content-encoding": "gzip",
                },
                body=compressed,
            )
        )

        request = self.driver.wait_for_request("/international/search/api/search/batchSearch?.*", timeout=0)

        self.assertEqual(request.body, b'{"search":true}')
        self.assertEqual(request.response.status_code, 200)
        self.assertEqual(request.response.body, compressed)
        self.assertEqual(request.response.headers.get("Content-Encoding"), "gzip")

    def test_plain_text_body_removes_stale_gzip_header(self):
        body = '{"status":0,"msg":"success"}'
        self.raw_driver.response_bodies["comfort"] = {"base64Encoded": False, "body": body}
        self.raw_driver.add_events(
            request_events(
                "comfort",
                "https://flights.test/search/api/flight/comfort/getFlightComfort",
                post_data='{"flightNoList":["MU5101"]}',
                response_headers={
                    "Content-Type": "application/json",
                    "content-encoding": "gzip",
                },
                body=body.encode("utf-8"),
            )
        )

        request = self.driver.wait_for_request("getFlightComfort", timeout=0)

        self.assertEqual(request.response.body, body.encode("utf-8"))
        self.assertNotIn("Content-Encoding", request.response.headers)

    def test_non_target_response_body_is_not_requested(self):
        self.raw_driver.add_events(
            request_events(
                "html",
                "https://flights.test/search/",
                response_headers={"Content-Type": "text/html"},
                body=b"page",
            )
        )

        requests = self.driver.requests

        self.assertEqual(len(requests), 1)
        self.assertIsNotNone(requests[0].response)
        self.assertFalse(
            any(command == "Network.getResponseBody" for command, _ in self.raw_driver.commands)
        )

    def test_deleting_requests_clears_capture(self):
        self.raw_driver.add_events(
            request_events(
                "html",
                "https://flights.test/search/",
                response_headers={"Content-Type": "text/html"},
            )
        )
        self.assertEqual(len(self.driver.requests), 1)

        del self.driver.requests

        self.assertEqual(self.driver.requests, [])

    def test_wait_times_out_without_matching_request(self):
        with self.assertRaises(TimeoutError):
            self.driver.wait_for_request("batchSearch", timeout=0)

    def test_other_webdriver_attributes_are_delegated(self):
        self.assertEqual(self.driver.title, "delegated property")

    def test_scrapers_use_native_capture_without_tls_bypass_flags(self):
        repository_root = Path(__file__).resolve().parents[1]
        scraper_paths = (
            repository_root / "ctrip_flights_scraper_V3.py",
            repository_root / "Linux_version" / "ctrip_flights_scraper_V3.5.py",
        )
        forbidden = (
            "seleniumwire",
            "--ignore-certificate-errors",
            "--ignore-certificate-errors-spki-list",
            "--ignore-ssl-errors",
        )

        for scraper_path in scraper_paths:
            source = scraper_path.read_text(encoding="utf-8")
            self.assertIn("BrowserNetworkCaptureDriver", source)
            for marker in forbidden:
                self.assertNotIn(marker, source)


if __name__ == "__main__":
    unittest.main()
