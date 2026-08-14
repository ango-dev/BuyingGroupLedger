import logging

from browser_use_sdk import BrowserUse as BrowserUseV2
from playwright.sync_api import sync_playwright

from models.profile import ProfileConfig

log = logging.getLogger(__name__)


class CdpBrowser:
    """Create a Browser-Use cloud browser pinned to a profile + proxy, connect Playwright over
    CDP for deterministic selector-based reads, and stop the cloud browser on exit.

    Uses raw HTTP (not the typed browsers.create()/stop()) to sidestep the SDK's strict
    proxy_cost/browser_cost regex, which rejects tiny values returned in scientific notation.

    Usage:
        with CdpBrowser(profile) as page:
            page.goto(url)
            ...
    """

    def __init__(self, profile: ProfileConfig):
        self.profile = profile
        self._client: BrowserUseV2 | None = None
        self._pw = None
        self._browser = None
        self._browser_id: str | None = None
        self.page = None

    def __enter__(self):
        self._client = BrowserUseV2()

        # NB: browserScreenWidth/Height are NOT set here. The API accepts them, but a profileId
        # OVERRIDES them — measured 2026-08-14: profile-less + 1024x768 gives screen 1024x768, while
        # profile + 1024x768 (even with allowResizing) still gives 1536x864. Every session here uses
        # a profile, so the setting would be inert. The default is fine anyway: 1536x864 @ dPR 1.25
        # is exactly what a 1920x1080 display at 125% scaling reports, the commonest desktop setup.
        body: dict = {}
        if self.profile.profile_id:
            body["profileId"] = self.profile.profile_id
        proxy = self.profile.proxy
        if proxy and proxy.host:
            body["customProxy"] = {
                "host": proxy.host,
                "port": proxy.port,
                "username": proxy.username or None,
                "password": proxy.password or None,
            }

        data = self._client._http.request("POST", "/browsers", json=body)
        self._browser_id = data["id"]

        # PAST THIS POINT A CLOUD BROWSER IS RUNNING AND BILLING, so every remaining step has to clean
        # up after itself. If connecting raises here, `with` never opens and __exit__ never runs — the
        # browser would be left running in the cloud, the Playwright driver subprocess left started,
        # and the SDK client left open. That orphaned driver is also the usual source of the
        # "Task was destroyed but it is pending" records that appear at interpreter shutdown.
        try:
            self._pw = sync_playwright().start()
            self._browser = self._pw.chromium.connect_over_cdp(data["cdpUrl"])
            ctx = self._browser.contexts[0] if self._browser.contexts else self._browser.new_context()

            # Block heavy resources we never read — cuts proxy bandwidth (billed per GB) and speeds
            # loads. Our reads use text selectors on the DOM, which resolve without images/media/fonts.
            try:
                ctx.route(
                    "**/*",
                    lambda route: route.abort()
                    if route.request.resource_type in ("image", "media", "font")
                    else route.continue_(),
                )
            except Exception:
                log.warning("Could not install resource-blocking route; continuing without it.",
                            exc_info=True)

            self.page = ctx.pages[0] if ctx.pages else ctx.new_page()
            return self.page
        except BaseException:
            # BaseException, not Exception: a KeyboardInterrupt or a driver-level failure mid-connect
            # must still hand the cloud browser back rather than leaking a paid session.
            log.warning("CDP connect failed after the browser was created; cleaning up.", exc_info=True)
            self.__exit__(None, None, None)
            raise

    def __exit__(self, *exc):
        try:
            if self._browser is not None:
                self._browser.close()
        except Exception:
            pass
        try:
            if self._pw is not None:
                self._pw.stop()
        except Exception:
            pass
        try:
            if self._browser_id:
                self._client._http.request("PATCH", f"/browsers/{self._browser_id}", json={"action": "stop"})
                log.info("Stopped CDP browser %s", self._browser_id)
        except Exception:
            log.warning("Failed to stop CDP browser %s", self._browser_id, exc_info=True)
        finally:
            if self._client is not None:
                self._client.close()
        return False
