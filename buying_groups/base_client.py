import requests


class BaseApiClient:
    """Thin HTTP client shared by buying-group API clients (BFMR, MaxOutDeals, ...)."""

    def __init__(self, base_url: str, api_key: str):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.api_key}"}

    def post(self, path: str, json: dict) -> dict:
        if not self.base_url or not self.api_key:
            raise RuntimeError(f"{type(self).__name__} is not configured (missing base URL or API key)")
        response = requests.post(f"{self.base_url}{path}", json=json, headers=self._headers(), timeout=15)
        response.raise_for_status()
        return response.json()
