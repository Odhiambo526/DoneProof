"""Independent read-only Chromium observations. No saved profiles or executor inputs."""
import asyncio
import os
import re
from dataclasses import dataclass

from .browser_network import BrowserNetwork, BrowserUnavailable

CHALLENGE = re.compile(r"\b(sign[ -]?in|log[ -]?in|captcha|verify (?:you are|your identity)|checking your browser|"
                       r"access denied|just a moment|consent required|accept (?:all )?cookies|"
                       r"two.factor|security check|enable cookies|authentication required)\b", re.I)


@dataclass(frozen=True)
class BrowserCapture:
    state: str
    png: bytes
    samples: int = 3


class ChromiumObserver:
    async def capture(self, check):
        from playwright.async_api import async_playwright
        # Neither a persistent profile nor remote debugging endpoint is accepted.
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True, chromium_sandbox=True,
                env={k: v for k, v in os.environ.items() if k.upper() in
                     {"PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "HOME", "LANG", "XDG_RUNTIME_DIR"}}, args=[
                "--host-resolver-rules=MAP * ~NOTFOUND", "--disable-quic",
                "--force-webrtc-ip-handling-policy=disable_non_proxied_udp",
            ])
            try:
                return await self.inspect(browser, check, BrowserNetwork(check))
            finally:
                await browser.close()

    async def inspect(self, browser, check, network):
        # This method is also used by the offline Chromium integration harness.
        context = await browser.new_context(viewport={"width": 1024, "height": 768}, device_scale_factor=1,
            service_workers="block", accept_downloads=False, permissions=[], java_script_enabled=True)
        failure = []
        def fail(code):
            if not failure:
                failure.append(code)
        try:
            context.set_default_timeout(2000)
            page = await context.new_page()

            async def block_socket(socket):
                fail("blocked_request")
                await socket.close(code=1008, reason="Read-only verifier")

            async def route_request(route):
                request = route.request
                try:
                    if (request.method != "GET" or request.url not in network.allowed
                            or request.frame != page.main_frame
                            or request.is_navigation_request() and request.url != check.url
                            or request.resource_type not in {"document", "script", "stylesheet", "image", "font", "fetch", "xhr"}):
                        raise BrowserUnavailable()
                    response = await network.get(request.url)
                    if request.is_navigation_request() and response.content_type != "text/html":
                        raise BrowserUnavailable("ambiguous_ui")
                    await route.fulfill(status=200, body=response.body, headers={"Content-Type": response.content_type,
                        "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
                        "Content-Security-Policy": "object-src 'none'; frame-src 'none'; worker-src 'none'; form-action 'none'; base-uri 'none'"})
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    fail(exc.code if isinstance(exc, BrowserUnavailable) else "blocked_request")
                    await route.abort()

            await context.route("**/*", route_request)
            await context.route_web_socket("**/*", block_socket)
            page.on("pageerror", lambda *_: fail("ambiguous_ui"))
            page.on("download", lambda *_: fail("blocked_request"))
            context.on("page", lambda *_: fail("blocked_request"))
            page.on("dialog", lambda *_: fail("login_or_challenge"))
            page.on("framenavigated", lambda frame: fail("blocked_request")
                    if frame == page.main_frame and frame.url != check.url else None)
            try:
                await page.goto(check.url, wait_until="networkidle", timeout=6500)
                states = []
                for _ in range(3):
                    states.append(await self.sample(page, check))
                    await asyncio.sleep(0.15)
                if failure:
                    raise BrowserUnavailable(failure[0])
                if len(set(states)) != 1:
                    raise BrowserUnavailable("unstable_ui")
                target = page.locator(check.selector)
                box = await target.bounding_box()
                if not box or box["width"] > 800 or box["height"] > 240:
                    raise BrowserUnavailable("screenshot_unavailable")
                png = await target.screenshot(type="png", animations="disabled", timeout=2000)
                if len(png) > 65536:
                    raise BrowserUnavailable("screenshot_unavailable")
                if await self.sample(page, check) != states[0] or failure:
                    raise BrowserUnavailable(failure[0] if failure else "unstable_ui")
                return BrowserCapture(states[0], png)
            except BrowserUnavailable:
                raise
            except Exception:
                raise BrowserUnavailable(failure[0] if failure else "ambiguous_ui") from None
        finally:
            await context.close()

    @staticmethod
    async def sample(page, check):
        if page.url != check.url or len(page.frames) != 1:
            raise BrowserUnavailable("blocked_request")
        # Only bounded text is inspected; raw page text is never returned or logged.
        text = await page.locator("body").evaluate("el => el.innerText.slice(0, 16384)")
        if any(marker in text for marker in ("doneproof.remediation", "action_hint", "reverify_after", "receipt_hash")):
            raise BrowserUnavailable("ambiguous_ui")
        if len(text) >= 16384 or CHALLENGE.search(text) or await page.locator(
                'input[type="password"],input[autocomplete="one-time-code"],iframe,dialog[open],'
                '[role="dialog"],[aria-busy="true"],form[action*="login"],form[action*="signin"]'
        ).count():
            raise BrowserUnavailable("login_or_challenge")
        values = []
        for selector in (check.page_marker, check.selector):
            element = page.locator(selector)
            if await element.count() != 1 or not await element.is_visible():
                raise BrowserUnavailable("ambiguous_ui")
            # Read visible plain text. Images, editable content and pseudo-element decorations cannot prove a state.
            value = await element.evaluate("""el => {
                const b=el.getBoundingClientRect(), s=getComputedStyle(el);
                const center=document.elementFromPoint(b.x+b.width/2,b.y+b.height/2);
                const unsafe=el.matches('input,textarea,select,canvas,img,video,svg,[contenteditable]') ||
                    el.querySelector('input,textarea,select,canvas,img,video,svg,[contenteditable]');
                return {text:el.innerText.slice(0,121), safe:!unsafe && s.opacity === '1' &&
                    s.visibility === 'visible' && s.backgroundImage === 'none' &&
                    ['none','normal'].includes(getComputedStyle(el,'::before').content) &&
                    ['none','normal'].includes(getComputedStyle(el,'::after').content) &&
                    b.x>=0 && b.y>=0 && b.right<=innerWidth && b.bottom<=innerHeight &&
                    (center===el || el.contains(center))};
            }""")
            if not value["safe"]:
                raise BrowserUnavailable("ambiguous_ui")
            values.append(value["text"].strip())
        if values[0] != check.page_text:
            raise BrowserUnavailable("ambiguous_ui")
        state = next((name for name, text in check.states.items() if text == values[1]), None)
        if state is None:
            raise BrowserUnavailable("ambiguous_ui")
        return state
