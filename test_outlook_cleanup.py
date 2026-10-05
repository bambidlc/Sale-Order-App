import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from src.outlook_fetcher import MicrosoftGraphClient, OutlookStateStore


class FakeResponse:
    def __init__(self, status_code=204, text="", headers=None):
        self.status_code = status_code
        self.text = text
        self.headers = headers or {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise AssertionError(f"HTTP {self.status_code}")


class FakeSession:
    def __init__(self):
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return FakeResponse()


class ThrottledSession(FakeSession):
    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if len(self.calls) == 1:
            return FakeResponse(status_code=429, headers={"Retry-After": "0"})
        return FakeResponse()


class OutlookCleanupTests(unittest.TestCase):
    def test_permanent_delete_uses_shop_message_action(self):
        settings = SimpleNamespace(
            tenant_id="tenant",
            client_id="client",
            client_secret="secret",
            user_email="shop@maderas3c.com",
        )
        client = MicrosoftGraphClient(settings)
        client.session = FakeSession()
        client._token = "token"

        client.permanent_delete_message("A/B+=")

        self.assertEqual(len(client.session.calls), 1)
        url = client.session.calls[0][0]
        self.assertEqual(
            url,
            "https://graph.microsoft.com/v1.0/users/shop@maderas3c.com/"
            "messages/A%2FB%2B%3D/permanentDelete",
        )

    def test_cleanup_queue_preserves_other_state_and_clears_exact_message(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            state_path = Path(temp_dir) / "state.json"
            store = OutlookStateStore(state_path)
            store.save_fetch({"attachment_id": "attachment-1"})
            store.queue_cleanup(
                {
                    "message_id": "message-1",
                    "attachment_id": "attachment-1",
                    "folder_name": "Reportes Ordenes",
                    "download_path": str(Path(temp_dir) / "report.xlsx"),
                },
                mailbox="shop@maderas3c.com",
            )

            self.assertEqual(store.pending_cleanup()["message_id"], "message-1")
            store.clear_pending_cleanup("another-message")
            self.assertIsNotNone(store.pending_cleanup())
            store.clear_pending_cleanup("message-1")
            self.assertIsNone(store.pending_cleanup())
            self.assertEqual(store.last_attachment(), "attachment-1")

    def test_permanent_delete_retries_mailbox_throttling(self):
        settings = SimpleNamespace(
            tenant_id="tenant",
            client_id="client",
            client_secret="secret",
            user_email="shop@maderas3c.com",
        )
        client = MicrosoftGraphClient(settings)
        client.session = ThrottledSession()
        client._token = "token"
        client.logger = SimpleNamespace(warning=lambda *args, **kwargs: None)

        original_sleep = time.sleep
        try:
            time.sleep = lambda _seconds: None
            client.permanent_delete_message("message-1")
        finally:
            time.sleep = original_sleep

        self.assertEqual(len(client.session.calls), 2)


if __name__ == "__main__":
    unittest.main()
