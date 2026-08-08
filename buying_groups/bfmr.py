import os

from buying_groups.base_client import BaseApiClient

# NOTE: endpoint path and payload shape are placeholders — wire up against BFMR's real API docs.


class BFMRClient(BaseApiClient):
    def __init__(self):
        super().__init__(
            base_url=os.getenv("BFMR_API_BASE_URL", ""),
            api_key=os.getenv("BFMR_API_KEY", ""),
        )

    def post_tracking(self, order_id: str, tracking_number: str) -> dict:
        return self.post(f"/reservations/{order_id}/tracking", {"tracking_number": tracking_number})
