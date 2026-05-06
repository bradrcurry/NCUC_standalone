from __future__ import annotations

import logging
from dataclasses import dataclass

import httpx

from duke_rates.config import Settings
from duke_rates.utils.retry import retry_call

logger = logging.getLogger(__name__)


@dataclass
class FetchResult:
    url: str
    status_code: int
    content_type: str
    text: str


class HttpNavigator:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.client = httpx.Client(
            follow_redirects=True,
            timeout=settings.request_timeout,
            headers={"User-Agent": settings.user_agent},
        )

    def fetch(self, url: str) -> FetchResult:
        response = retry_call(
            lambda: self.client.get(url),
            retries=self.settings.max_retries,
            delay_seconds=self.settings.rate_limit_seconds,
            retry_on=(httpx.HTTPError,),
        )
        return FetchResult(
            url=str(response.url),
            status_code=response.status_code,
            content_type=response.headers.get("content-type", ""),
            text=response.text,
        )

    def close(self) -> None:
        self.client.close()


class PlaywrightNavigator:
    def __init__(self, settings: Settings):
        self.settings = settings

    def fetch(self, url: str) -> FetchResult:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:  # pragma: no cover - optional dependency
            raise RuntimeError(
                "Playwright is not installed. Install duke-rates[browser]."
            ) from exc

        logger.info("Rendering page with Playwright: %s", url)
        with sync_playwright() as playwright:  # pragma: no cover - optional dependency
            browser = playwright.chromium.launch(headless=True)
            page = browser.new_page(user_agent=self.settings.user_agent)
            response = page.goto(
                url, wait_until="networkidle", timeout=int(self.settings.request_timeout * 1000)
            )
            content = page.content()
            final_url = page.url
            browser.close()
            return FetchResult(
                url=final_url,
                status_code=response.status if response else 200,
                content_type="text/html",
                text=content,
            )
