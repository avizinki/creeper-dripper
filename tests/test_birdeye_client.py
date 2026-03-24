from __future__ import annotations

from creeper_dripper.clients.birdeye import BirdeyeClient


def test_trending_tokens_clamps_limit_and_omits_interval(monkeypatch):
    client = BirdeyeClient("test", min_interval_s=0.0)
    captured = {}

    class DummyResponse:
        status_code = 200
        text = ""

        def raise_for_status(self):
            return None

        def json(self):
            return {"success": True, "data": {"tokens": []}}

    def fake_request(method, url, params=None, timeout=20):
        captured["method"] = method
        captured["url"] = url
        captured["params"] = params
        return DummyResponse()

    monkeypatch.setattr(client._session, "request", fake_request)
    out = client.trending_tokens(limit=200)
    assert out == []
    assert captured["url"].endswith("/defi/token_trending")
    assert captured["params"]["limit"] == 20
    assert "interval" not in captured["params"]
