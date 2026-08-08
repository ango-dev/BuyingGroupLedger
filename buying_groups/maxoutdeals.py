import os

from buying_groups.base_client import BaseApiClient

# NOTE: endpoint path and payload shape are placeholders — wire up against MaxOutDeals' real API docs.


class MaxOutDealsClient(BaseApiClient):
    def __init__(self):
        super().__init__(
            base_url=os.getenv("MAXOUTDEALS_API_BASE_URL", ""),
            api_key=os.getenv("MAXOUTDEALS_API_KEY", ""),
        )

    def submit_tracking(self, order_id: str, tracking_number: str) -> dict:
        return self.post(f"/orders/{order_id}/tracking", {"tracking_number": tracking_number})
