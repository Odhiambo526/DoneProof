"""Real sandboxed Chromium, entirely offline HTML fixtures; opt in on a browser worker."""
import asyncio
import os

import pytest

from doneproof.browser_network import BrowserResponse, BrowserUnavailable
from doneproof.browser_policy import BrowserCheck
from doneproof.browser_runner import ChromiumObserver
from tests.browser_helpers import CHECK

pytestmark = pytest.mark.skipif(os.getenv("DONEPROOF_BROWSER_TESTS") != "1", reason="Run browser worker Chromium integration job")
HTML = '<!doctype html><html><body><h1 id="release-id">Release 7</h1><p id="release-status" style="width:fit-content">Complete</p></body></html>'


class OfflineNetwork:
    def __init__(self, html):
        self.html = html
        self.allowed = frozenset({CHECK["url"]})
        self.calls = []

    async def get(self, url):
        assert url in self.allowed
        self.calls.append(url)
        return BrowserResponse(self.html.encode(), "text/html")


async def collect(html, *, repeat=False):
    from playwright.async_api import async_playwright
    network = OfflineNetwork(html)
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, chromium_sandbox=True,
            args=["--host-resolver-rules=MAP * ~NOTFOUND", "--disable-quic",
                  "--force-webrtc-ip-handling-policy=disable_non_proxied_udp"])
        try:
            observer = ChromiumObserver()
            result = await observer.inspect(browser, BrowserCheck.model_validate(CHECK), network)
            assert browser.contexts == []
            if repeat:
                again = await observer.inspect(browser, BrowserCheck.model_validate(CHECK), network)
                assert again.state == result.state and browser.contexts == []
            return result, network
        finally:
            await browser.close()


def test_chromium_captures_bounded_independent_pixels():
    result, network = asyncio.run(collect(HTML))
    assert result.state == "ready" and result.samples == 3
    assert result.png.startswith(b"\x89PNG\r\n\x1a\n") and 8 < len(result.png) <= 65536
    assert network.calls == [CHECK["url"]]


def test_chromium_does_not_reuse_cookies_or_storage_between_observations():
    script = """<script>
    if(localStorage.getItem('seen') || document.cookie.includes('seen='))
        document.querySelector('#release-status').textContent='Pending';
    localStorage.setItem('seen','yes'); document.cookie='seen=yes';
    </script>"""
    result, network = asyncio.run(collect(HTML.replace("</body>", script + "</body>"), repeat=True))
    assert result.state == "ready" and len(network.calls) == 2


def test_chromium_recognizes_explicit_negative_state():
    result, _ = asyncio.run(collect(HTML.replace("Complete", "Pending")))
    assert result.state == "pending"


@pytest.mark.parametrize("html,code", [
    (HTML.replace("Complete", "Finishing soon"), "ambiguous_ui"),
    (HTML.replace("Release 7", "Release 8"), "ambiguous_ui"),
    (HTML.replace('</body>', '<p id="release-status">Complete</p></body>'), "ambiguous_ui"),
    (HTML.replace('id="release-status"', 'id="release-status" style="display:none"'), "ambiguous_ui"),
    (HTML.replace('<body>', '<body style="opacity:0">'), "ambiguous_ui"),
    (HTML.replace('<body>', '<body style="animation:fade 2s infinite"><style>@keyframes fade {from {opacity:1} to {opacity:0}}</style>'), "ambiguous_ui"),
    (HTML.replace('</body>', '<input type="password"></body>'), "login_or_challenge"),
    (HTML.replace('</body>', '<p>Verify you are human</p></body>'), "login_or_challenge"),
    (HTML.replace('</body>', '<dialog open>Accept all cookies</dialog></body>'), "login_or_challenge"),
    (HTML.replace('</body>', '<p>doneproof.remediation action_hint Complete</p></body>'), "ambiguous_ui"),
    (HTML.replace('</body>', '<img src="https://other.example.org/pixel"></body>'), "blocked_request"),
    (HTML.replace('</body>', '<script>fetch(location.href,{method:"POST"}).catch(()=>{})</script></body>'), "blocked_request"),
], ids=["unknown-state", "wrong-page", "duplicate", "hidden", "transparent-parent", "animated-parent", "login", "challenge", "interstitial", "guidance", "unlisted-resource", "write-request"])
def test_chromium_returns_unknown_for_inconclusive_or_disallowed_ui(html, code):
    with pytest.raises(BrowserUnavailable) as exc:
        asyncio.run(collect(html))
    assert exc.value.code == code


def test_chromium_observes_stability_across_multiple_samples():
    html = HTML.replace('</body>', '<script>setInterval(()=>{const s=document.querySelector("#release-status");'
        's.textContent=s.textContent==="Complete"?"Pending":"Complete"},100)</script></body>')
    with pytest.raises(BrowserUnavailable) as exc:
        asyncio.run(collect(html))
    assert exc.value.code == "unstable_ui"


def test_transient_state_changes_cannot_hide_by_reverting_between_samples():
    # Each JS task returns to Complete before the next sample can execute.
    # The previous sample-only implementation incorrectly accepted this fixture.
    html = HTML.replace('</body>', '<script>setInterval(()=>{const s=document.querySelector("#release-status");'
        's.textContent="Pending";s.textContent="Complete"},100)</script></body>')
    with pytest.raises(BrowserUnavailable) as exc:
        asyncio.run(collect(html))
    assert exc.value.code == "unstable_ui"


def test_chromium_cancellation_discards_the_context():
    from playwright.async_api import async_playwright
    class Pending(OfflineNetwork):
        async def get(self, url):
            await asyncio.sleep(30)
    async def cancel():
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True, chromium_sandbox=True)
            try:
                task = asyncio.create_task(ChromiumObserver().inspect(browser, BrowserCheck.model_validate(CHECK), Pending(HTML)))
                await asyncio.sleep(0.5)
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
                assert browser.contexts == []
            finally:
                await browser.close()
    asyncio.run(cancel())


def test_connection_session_console_uses_safe_text_and_pinned_receipt():
    import json
    from pathlib import Path

    from doneproof.assurance_models import assurance_summary
    from doneproof.domain import VerificationReceipt
    from doneproof.session_web import SESSION_JS, SESSION_PANEL
    fixtures = json.loads((Path(__file__).parents[1] / 'sdk/typescript/test/fixtures.json').read_text())
    item = fixtures['receipts'][2]
    receipt = VerificationReceipt.model_validate(item['receipt'])
    session = {**fixtures['ready_session'], 'state': receipt.verdict.value, 'receipt': item['receipt'],
               'assurance': assurance_summary(receipt).model_dump(), 'signed_payload_b64': item['signed_payload_b64']}
    session['baselines'] = [{'id': 'p1', 'status': 'FAIL', 'reason': '<img src=x onerror=alert(1)>'}]
    async def run():
        from playwright.async_api import async_playwright
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True, chromium_sandbox=True)
            try:
                page = await browser.new_page()
                errors = []
                page.on('pageerror', lambda error: errors.append(type(error).__name__))
                async def route(request):
                    url = request.request.url
                    if url.endswith('/console'):
                        await request.fulfill(content_type='text/html', body='<input id="key" type="password">' + SESSION_PANEL + '<script src="/session.js"></script>')
                    elif url.endswith('/session.js'):
                        await request.fulfill(content_type='text/javascript', body=SESSION_JS)
                    elif url.endswith('/v1/assurance/sessions/' + session['id']):
                        await request.fulfill(content_type='application/json', body=json.dumps(session))
                    else:
                        await request.abort()
                await page.route('**/*', route)
                await page.goto('https://doneproof.test/console')
                await page.fill('#session-id', session['id'])
                await page.click('#session-load')
                await page.wait_for_function("document.querySelector('#session-details').textContent.includes('Baseline p1')")
                assert await page.locator('#session-details img').count() == 0
                assert 'lower assurance' in await page.locator('#session-details').inner_text()
                await page.fill('#session-pin', item['pinned_public_key'])
                await page.click('#session-trust')
                await page.wait_for_function("document.querySelector('#session-signature').textContent.startsWith('Signature verified')")
                await page.fill('#session-pin', 'untrusted')
                await page.click('#session-trust')
                await page.wait_for_function("document.querySelector('#session-signature').textContent.startsWith('Pinned signature verification failed')")
                assert not errors
            finally:
                await browser.close()
    asyncio.run(run())
