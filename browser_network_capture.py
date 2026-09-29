"""Chromium DevTools network capture for Selenium WebDriver.

This adapter uses browser performance logs and CDP Network events directly. It
never inserts a TLS-intercepting proxy and leaves the browser's certificate
verification behavior unchanged.
"""

from __future__ import annotations

import base64
import json
import re
import time
from dataclasses import dataclass, field
from typing import Any


_BODY_URL_FRAGMENTS = (
    "/international/search/api/search/batchSearch",
    "/search/api/flight/comfort/",
)
_GZIP_MAGIC = b"\x1f\x8b"


@dataclass
class CapturedResponse:
    status_code: int
    headers: dict[str, Any]
    body: bytes = b""


@dataclass
class CapturedRequest:
    url: str
    method: str
    headers: dict[str, Any] = field(default_factory=dict)
    body: bytes = b""
    response: CapturedResponse | None = None
    _body_ready: bool = False
    _body_error: Exception | None = field(default=None, repr=False)


class BrowserNetworkCaptureDriver:
    """Expose the legacy request collection API over Chromium's native CDP logs.

    Existing scraper code can continue using ``driver.requests``,
    ``del driver.requests``, and ``driver.wait_for_request(pattern, timeout)``.
    All other WebDriver operations are delegated to the wrapped Selenium driver.
    """

    def __init__(self, driver: Any) -> None:
        self._driver = driver
        self._requests: list[CapturedRequest] = []
        self._requests_by_id: dict[str, CapturedRequest] = {}

        # CDP commands run inside the browser session. No external proxy or
        # certificate override is configured here.
        self._driver.execute_cdp_cmd("Network.enable", {})
        self._driver.get_log("performance")  # discard startup/navigation noise

    def __getattr__(self, name: str) -> Any:
        return getattr(self._driver, name)

    @property
    def requests(self) -> list[CapturedRequest]:
        self._collect_events()
        return list(self._requests)

    @requests.deleter
    def requests(self) -> None:
        self.clear_requests()

    def clear_requests(self) -> None:
        """Discard captured entries and any already-buffered performance logs."""
        self._collect_events()
        self._requests.clear()
        self._requests_by_id.clear()
        self._driver.get_log("performance")

    def wait_for_request(self, pattern: str, timeout: float | None = 10) -> CapturedRequest:
        """Wait for a matching request whose response body has been captured."""
        matcher = re.compile(pattern)
        deadline = None if timeout is None else time.monotonic() + max(0, timeout)

        while True:
            self._collect_events()
            for request in reversed(self._requests):
                if not matcher.search(request.url):
                    continue
                if request._body_error is not None:
                    raise RuntimeError(
                        f"Could not retrieve response body for {request.url}: "
                        f"{request._body_error}"
                    ) from request._body_error
                if request.response is not None and request._body_ready:
                    return request

            if deadline is not None and time.monotonic() >= deadline:
                raise TimeoutError(f"Timed out waiting for a response matching {pattern!r}")
            time.sleep(0.1)

    def _collect_events(self) -> None:
        for entry in self._driver.get_log("performance"):
            event = self._decode_performance_entry(entry)
            if event is None:
                continue

            method = event.get("method")
            params = event.get("params", {})
            request_id = params.get("requestId")
            if not request_id:
                continue
            request_id = str(request_id)

            if method == "Network.requestWillBeSent":
                info = params.get("request", {})
                post_data = info.get("postData", "")
                request = CapturedRequest(
                    url=info.get("url", ""),
                    method=info.get("method", "GET"),
                    headers=dict(info.get("headers", {})),
                    body=post_data.encode("utf-8") if isinstance(post_data, str) else b"",
                )
                self._requests.append(request)
                self._requests_by_id[request_id] = request
                continue

            request = self._requests_by_id.get(request_id)
            if request is None:
                continue

            if method == "Network.responseReceived":
                response_info = params.get("response", {})
                request.response = CapturedResponse(
                    status_code=int(response_info.get("status", 0)),
                    headers={
                        str(name).title(): value
                        for name, value in response_info.get("headers", {}).items()
                    },
                )
            elif method == "Network.loadingFinished":
                self._retrieve_request_body(request_id, request)
            elif method == "Network.loadingFailed":
                request._body_error = RuntimeError(params.get("errorText", "network request failed"))

    @staticmethod
    def _decode_performance_entry(entry: dict[str, Any]) -> dict[str, Any] | None:
        raw_message = entry.get("message")
        try:
            payload = json.loads(raw_message) if isinstance(raw_message, str) else raw_message
        except (TypeError, json.JSONDecodeError):
            return None

        # ChromeDriver/EdgeDriver wrap the CDP event in a WebDriver log envelope.
        while isinstance(payload, dict) and "method" not in payload:
            nested = payload.get("message")
            if not isinstance(nested, dict):
                return None
            payload = nested
        return payload if isinstance(payload, dict) and "method" in payload else None

    def _retrieve_request_body(self, request_id: str, request: CapturedRequest) -> None:
        if request.body == b"" and any(fragment in request.url for fragment in _BODY_URL_FRAGMENTS):
            try:
                post_data = self._driver.execute_cdp_cmd(
                    "Network.getRequestPostData", {"requestId": request_id}
                ).get("postData", "")
                if isinstance(post_data, str):
                    request.body = post_data.encode("utf-8")
            except Exception:
                # The request may be a GET, may have no body, or may already have
                # been released by Chromium. Response parsing remains independent.
                pass

        if request.response is None or not any(
            fragment in request.url for fragment in _BODY_URL_FRAGMENTS
        ):
            return

        try:
            result = self._driver.execute_cdp_cmd(
                "Network.getResponseBody", {"requestId": request_id}
            )
            body = result.get("body", "")
            if result.get("base64Encoded"):
                response_body = base64.b64decode(body)
            else:
                response_body = body.encode("utf-8") if isinstance(body, str) else bytes(body)

            self._normalize_content_encoding(request.response.headers, response_body)
            request.response.body = response_body
            request._body_ready = True
        except Exception as exc:
            request._body_error = exc

    @staticmethod
    def _normalize_content_encoding(headers: dict[str, Any], body: bytes) -> None:
        """Keep gzip metadata consistent when Chromium has decoded the body."""
        encoding_key = next(
            (key for key in headers if key.lower() == "content-encoding"), None
        )
        if encoding_key is None:
            return
        encoding = str(headers[encoding_key]).lower()
        if "gzip" in encoding and not body.startswith(_GZIP_MAGIC):
            del headers[encoding_key]
