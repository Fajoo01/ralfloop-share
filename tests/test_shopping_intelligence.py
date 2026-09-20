from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

from ralfloop_agent.shopping_intelligence.api import create_app
from ralfloop_agent.shopping_intelligence.connectors import EbayConnector, StructuredDataConnector
from ralfloop_agent.shopping_intelligence.models import SearchRequest
from ralfloop_agent.shopping_intelligence.service import ShoppingService
from ralfloop_agent.shopping_intelligence.storage import ShoppingStore


class FakeResponse:
    def __init__(self, payload=None, *, text="", url="https://shop.example/item", status_code=200):
        self._payload = payload
        self.text = text
        self.url = url
        self.status_code = status_code

    def json(self):
        return self._payload


class FakeEbaySession:
    def post(self, url, **kwargs):
        return FakeResponse({"access_token": "test-token", "expires_in": 7200})

    def get(self, url, **kwargs):
        return FakeResponse(
            {
                "itemSummaries": [
                    {
                        "itemId": "v1|123|0",
                        "title": "Acme Widget 256 GB Black",
                        "itemWebUrl": "https://www.ebay.it/itm/123",
                        "price": {"value": "249.00", "currency": "EUR"},
                        "shippingOptions": [{"shippingCost": {"value": "5.90", "currency": "EUR"}}],
                        "condition": "New",
                        "seller": {"username": "seller1", "feedbackPercentage": "99.8"},
                        "gtin": "8001234567890",
                        "localizedAspects": [
                            {"name": "Brand", "value": "Acme"},
                            {"name": "MPN", "value": "WIDGET-256-BLK"},
                            {"name": "Model", "value": "Widget 256"},
                        ],
                    }
                ]
            }
        )


class FakeStructuredSession:
    def __init__(self, html: str):
        self.html = html

    def get(self, url, **kwargs):
        return FakeResponse(text=self.html, url=url)


PRODUCT_HTML = """<!doctype html><html><head>
<script type="application/ld+json">
{
  "@context": "https://schema.org",
  "@type": "Product",
  "name": "Acme Widget 256GB - Nero",
  "brand": {"@type": "Brand", "name": "Acme"},
  "gtin13": "8001234567890",
  "mpn": "WIDGET-256-BLK",
  "model": "Widget 256",
  "offers": {
    "@type": "Offer",
    "url": "https://shop.example/widget-256",
    "price": "244.50",
    "priceCurrency": "EUR",
    "availability": "https://schema.org/InStock",
    "itemCondition": "https://schema.org/NewCondition",
    "shippingDetails": {"shippingRate": {"value": "4.90", "currency": "EUR"}}
  }
}
</script></head><body></body></html>"""


def build_service(db_path: Path) -> ShoppingService:
    ebay = EbayConnector(session=FakeEbaySession())
    ebay.client_id = "client"
    ebay.client_secret = "secret"
    structured = StructuredDataConnector(session=FakeStructuredSession(PRODUCT_HTML))
    return ShoppingService(ShoppingStore(str(db_path)), [ebay, structured])


def test_multisource_resolution_and_history(tmp_path):
    service = build_service(tmp_path / "shopping.sqlite")
    request = SearchRequest(
        query="Acme Widget 256",
        urls=["https://example.com/widget-256"],
        limit=10,
    )

    first = service.search(request)
    assert len(first.offers) == 2
    assert len(first.products) == 1
    assert {offer.source for offer in first.offers} == {"ebay", "structured-data"}
    assert {offer.canonical_product_id for offer in first.offers} == {first.products[0].id}
    assert sorted(str(offer.total_cost) for offer in first.offers) == ["249.40", "254.90"]
    assert first.products[0].identity_key == "gtin:8001234567890"

    second = service.search(request)
    assert second.observations_written == 2
    history = service.store.history(first.products[0].id)
    assert len(history) == 4
    assert {point.source for point in history} == {"ebay", "structured-data"}


def test_structured_data_parser_extracts_identifiers_and_shipping():
    offers = StructuredDataConnector.parse_html(PRODUCT_HTML, "https://example.com/widget-256")
    assert len(offers) == 1
    offer = offers[0]
    assert offer.gtin == "8001234567890"
    assert offer.mpn == "WIDGET-256-BLK"
    assert str(offer.shipping_cost) == "4.90"
    assert str(offer.total_cost) == "249.40"


def test_post_search_api(tmp_path):
    service = build_service(tmp_path / "shopping.sqlite")
    client = TestClient(create_app(service=service))
    response = client.post(
        "/search",
        json={
            "query": "Acme Widget 256",
            "urls": ["https://example.com/widget-256"],
            "limit": 10,
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["observations_written"] == 2
    assert len(body["products"]) == 1
    assert len(body["offers"]) == 2

    product_id = body["products"][0]["id"]
    history = client.get(f"/products/{product_id}/history")
    assert history.status_code == 200
    assert len(history.json()) == 2


def test_connector_failure_is_isolated(tmp_path):
    ebay = EbayConnector(session=FakeEbaySession())
    ebay.client_id = None
    ebay.client_secret = None
    structured = StructuredDataConnector(session=FakeStructuredSession(PRODUCT_HTML))
    service = ShoppingService(ShoppingStore(str(tmp_path / "shopping.sqlite")), [ebay, structured])
    result = service.search(
        SearchRequest(query="Acme Widget", urls=["https://example.com/widget"], limit=5)
    )
    assert len(result.offers) == 1
    assert result.offers[0].source == "structured-data"
    assert result.connector_results[0].error is not None
    assert result.connector_results[1].error is None


def test_opengraph_fallback_marks_unknown_shipping():
    html = '<meta property="og:title" content="Demo"><meta property="og:price:amount" content="12.34"><meta property="og:price:currency" content="EUR">'
    offers = StructuredDataConnector.parse_html(html, "https://example.com/demo")
    assert len(offers) == 1
    assert offers[0].shipping_cost is None
    assert offers[0].unknown_cost_components == ["shipping"]
    assert str(offers[0].total_cost) == "12.34"
