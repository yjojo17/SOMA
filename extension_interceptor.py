"""
Extension-based Instagram response capture.

Drop-in replacement for SeleniumWireInterceptor that sources raw API response
bodies from the in-browser StreamFilter extension (ig_capture_extension/)
instead of a selenium-wire MITM proxy.

Why: selenium-wire terminates TLS in a local proxy, so the handshake Instagram
sees is mitmproxy's, not Firefox's — a TLS/UA mismatch that fires before any
JS runs. Capturing inside the browser keeps Firefox's genuine TLS handshake.

All response parsing is inherited unchanged from SeleniumWireInterceptor; only
the ingestion path differs (DOM relay drain vs. driver.requests).
"""
import json
import logging

from seleniumwire_interceptor import SeleniumWireInterceptor

# Reads and clears the relay node in a single synchronous step. JS is
# single-threaded, so the content script's onMessage handler cannot run between
# the read and the clear — no capture is lost or double-read.
_DRAIN_JS = r"""
var n = document.getElementById("__ig_capture_relay");
if (!n) { return ""; }
var t = n.textContent;
n.textContent = "";
return t;
"""


class ExtensionInterceptor(SeleniumWireInterceptor):
    """Same parsing as SeleniumWireInterceptor, fed from the capture extension."""

    def __init__(self, archive=None):
        super().__init__(archive=archive)
        self.logger = logging.getLogger("instagram_network")

    def process_requests(self, driver):
        """Drain the relay node and feed each response through the shared parser."""
        raw = driver.execute_script(_DRAIN_JS)
        if not raw:
            return

        for line in raw.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
                url = record["url"]
                data = json.loads(record["body"])
            except (ValueError, KeyError) as e:
                self.logger.debug(f"Skipping malformed relay line: {e}")
                continue

            if self.archive is not None:
                self.archive.append(url, data)
            self._process_api_response(url, data)