"""Exercise the production launch/cleanup path against a fully offline UI fixture."""
import asyncio
from unittest.mock import patch

from doneproof.browser_network import BrowserResponse
from doneproof.browser_policy import BrowserCheck
from doneproof.browser_runner import ChromiumObserver

URL = "https://ui.example.org/release/7"


class OfflineNetwork:
    def __init__(self, check):
        self.allowed = frozenset({URL})

    async def get(self, url):
        assert url == URL
        return BrowserResponse(b'<!doctype html><html><body><h1 id="release">Release 7</h1>'
            b'<span id="status">Complete</span></body></html>', "text/html")


async def main():
    check = BrowserCheck.model_validate({"url": URL, "page_marker": "#release", "page_text": "Release 7",
        "selector": "#status", "states": {"ready": "Complete", "pending": "Pending"},
        "success_state": "ready", "no_authoritative_api": True})
    with patch("doneproof.browser_runner.BrowserNetwork", OfflineNetwork):
        result = await ChromiumObserver().capture(check)
    assert result.state == "ready" and result.samples == 3 and 8 < len(result.png) <= 65536
    print("Independent sandboxed Chromium: recognized state, bounded screenshot, cleanup passed.")


if __name__ == "__main__":
    asyncio.run(main())
