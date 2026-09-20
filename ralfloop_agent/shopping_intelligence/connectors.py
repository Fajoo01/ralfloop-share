from __future__ import annotations

import base64
import hashlib
import ipaddress
import json
import os
import socket
import time
from abc import ABC, abstractmethod
from decimal import Decimal, InvalidOperation
from html.parser import HTMLParser
from typing import Iterable
from urllib.parse import urlparse

import requests

from .models import Offer, SearchRequest


class ConnectorError(RuntimeError):
    pass


class ShoppingConnector(ABC):
    name: str

    @abstractmethod
    def search(self, request: SearchRequest) -> list[Offer]:
        raise NotImplementedError


def _decimal(value: object, default: str = "0") -> Decimal:
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return Decimal(default)


def _payload_hash(payload: object) -> str:
    encoded = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str).encode()
    return hashlib.sha256(encoded).hexdigest()


def _first_text(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, list):
        return _first_text(value[0]) if value else None
    text = str(value).strip()
    return text or None


def _aspect(item: dict, *names: str) -> str | None:
    wanted = {name.casefold() for name in names}
    for aspect in item.get("localizedAspects") or []:
        if str(aspect.get("name", "")).casefold() in wanted:
            return _first_text(aspect.get("value"))
    return None


class EbayConnector(ShoppingConnector):
    name = "ebay"

    def __init__(self, session: requests.Session | None = None) -> None:
        self.session = session or requests.Session()
        self.client_id = os.getenv("EBAY_CLIENT_ID")
        self.client_secret = os.getenv("EBAY_CLIENT_SECRET")
        self.marketplace = os.getenv("EBAY_MARKETPLACE_ID", "EBAY_IT")
        environment = os.getenv("EBAY_ENVIRONMENT", "production").casefold()
        host = "api.sandbox.ebay.com" if environment == "sandbox" else "api.ebay.com"
        self.token_url = f"https://{host}/identity/v1/oauth2/token"
        self.search_url = f"https://{host}/buy/browse/v1/item_summary/search"
        self._token: str | None = None
        self._token_expires_at = 0.0

    @property
    def configured(self) -> bool:
        return bool(self.client_id and self.client_secret)

    def _access_token(self) -> str:
        if not self.configured:
            raise ConnectorError("eBay connector not configured: set EBAY_CLIENT_ID and EBAY_CLIENT_SECRET")
        if self._token and time.time() < self._token_expires_at:
            return self._token
        raw = f"{self.client_id}:{self.client_secret}".encode()
        basic = base64.b64encode(raw).decode()
        response = self.session.post(
            self.token_url,
            headers={
                "Authorization": f"Basic {basic}",
                "Content-Type": "application/x-www-form-urlencoded",
            },
            data={
                "grant_type": "client_credentials",
                "scope": "https://api.ebay.com/oauth/api_scope",
            },
            timeout=15,
        )
        if response.status_code >= 400:
            raise ConnectorError(f"eBay OAuth failed with HTTP {response.status_code}")
        payload = response.json()
        self._token = payload["access_token"]
        self._token_expires_at = time.time() + max(30, int(payload.get("expires_in", 7200)) - 60)
        return self._token

    def search(self, request: SearchRequest) -> list[Offer]:
        token = self._access_token()
        response = self.session.get(
            self.search_url,
            params={"q": request.query, "limit": request.limit},
            headers={
                "Authorization": f"Bearer {token}",
                "X-EBAY-C-MARKETPLACE-ID": self.marketplace,
            },
            timeout=20,
        )
        if response.status_code >= 400:
            raise ConnectorError(f"eBay Browse search failed with HTTP {response.status_code}")
        return [self._normalize_item(item) for item in response.json().get("itemSummaries", [])]

    def _normalize_item(self, item: dict) -> Offer:
        item_price = _decimal((item.get("price") or {}).get("value"))
        currency = str((item.get("price") or {}).get("currency") or "EUR")
        shipping_values = []
        for option in item.get("shippingOptions") or []:
            cost = option.get("shippingCost") or {}
            if cost.get("value") is not None:
                shipping_values.append(_decimal(cost.get("value")))
        shipping = min(shipping_values) if shipping_values else None
        seller = item.get("seller") or {}
        gtin = _first_text(item.get("gtin"))
        return Offer(
            source=self.name,
            source_listing_id=str(item.get("itemId") or _payload_hash(item)[:24]),
            title=str(item.get("title") or "Untitled eBay item"),
            url=str(item.get("itemWebUrl") or item.get("itemAffiliateWebUrl") or ""),
            condition=_first_text(item.get("condition")),
            seller=_first_text(seller.get("username")),
            seller_reputation=float(seller["feedbackPercentage"]) if seller.get("feedbackPercentage") else None,
            item_price=item_price,
            shipping_cost=shipping,
            unknown_cost_components=[] if shipping is not None else ["shipping"],
            total_cost=item_price + (shipping or Decimal("0")),
            currency=currency,
            availability="available" if not item.get("buyingOptions") else ",".join(item.get("buyingOptions")),
            raw_source_hash=_payload_hash(item),
            brand=_aspect(item, "Brand", "Marca"),
            gtin=gtin,
            mpn=_aspect(item, "MPN", "Manufacturer Part Number", "Numero parte produttore"),
            model=_aspect(item, "Model", "Modello"),
        )


class _JsonLdParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.blocks: list[str] = []
        self.meta: dict[str, str] = {}
        self._capture = False
        self._parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr_map = {key.casefold(): value for key, value in attrs}
        if tag.casefold() == "meta":
            key = (attr_map.get("property") or attr_map.get("name") or "").casefold()
            content = attr_map.get("content")
            if key and content:
                self.meta[key] = content
            return
        if tag.casefold() != "script":
            return
        if (attr_map.get("type") or "").casefold() == "application/ld+json":
            self._capture = True
            self._parts = []

    def handle_data(self, data: str) -> None:
        if self._capture:
            self._parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.casefold() == "script" and self._capture:
            self.blocks.append("".join(self._parts))
            self._capture = False
            self._parts = []


def _iter_jsonld_nodes(value: object) -> Iterable[dict]:
    if isinstance(value, list):
        for item in value:
            yield from _iter_jsonld_nodes(item)
    elif isinstance(value, dict):
        yield value
        graph = value.get("@graph")
        if graph is not None:
            yield from _iter_jsonld_nodes(graph)


def _type_is(value: object, wanted: str) -> bool:
    values = value if isinstance(value, list) else [value]
    return any(str(item).split("/")[-1].casefold() == wanted.casefold() for item in values if item)


def _validate_fetch_url(url: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ConnectorError("structured-data connector accepts only http(s) URLs")
    if os.getenv("SHOPPING_ALLOW_PRIVATE_URLS") == "1":
        return
    try:
        addresses = {info[4][0] for info in socket.getaddrinfo(parsed.hostname, None)}
    except socket.gaierror as exc:
        raise ConnectorError(f"cannot resolve URL host: {parsed.hostname}") from exc
    for address in addresses:
        ip = ipaddress.ip_address(address)
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
            raise ConnectorError("private/link-local product URLs are disabled")


class StructuredDataConnector(ShoppingConnector):
    name = "structured-data"

    def __init__(self, session: requests.Session | None = None) -> None:
        self.session = session or requests.Session()

    def search(self, request: SearchRequest) -> list[Offer]:
        urls = list(request.urls)
        if request.query.startswith(("http://", "https://")) and request.query not in urls:
            urls.append(request.query)
        offers: list[Offer] = []
        for url in urls:
            offers.extend(self._fetch_url(url))
        return offers[: request.limit]

    def _fetch_url(self, url: str) -> list[Offer]:
        _validate_fetch_url(url)
        response = self.session.get(
            url,
            headers={"User-Agent": "Bot-tazzi-Shopping/0.1 (+self-hosted price intelligence)"},
            timeout=20,
        )
        if response.status_code >= 400:
            raise ConnectorError(f"structured-data fetch failed with HTTP {response.status_code}")
        return self.parse_html(response.text, response.url or url)

    @classmethod
    def parse_html(cls, html: str, page_url: str) -> list[Offer]:
        parser = _JsonLdParser()
        parser.feed(html)
        products: list[dict] = []
        for block in parser.blocks:
            try:
                data = json.loads(block)
            except json.JSONDecodeError:
                continue
            for node in _iter_jsonld_nodes(data):
                if _type_is(node.get("@type"), "Product"):
                    products.append(node)
        offers: list[Offer] = []
        for product in products:
            offers.extend(cls._normalize_product(product, page_url))
        if offers:
            return offers
        meta_offer = cls._normalize_opengraph(parser.meta, page_url)
        return [meta_offer] if meta_offer else []

    @classmethod
    def _normalize_product(cls, product: dict, page_url: str) -> list[Offer]:
        raw_offers = product.get("offers") or []
        if isinstance(raw_offers, dict):
            raw_offers = [raw_offers]
        brand = product.get("brand")
        if isinstance(brand, dict):
            brand = brand.get("name")
        normalized: list[Offer] = []
        for index, offer in enumerate(raw_offers):
            if not isinstance(offer, dict):
                continue
            price = offer.get("price")
            currency = offer.get("priceCurrency")
            if price is None and isinstance(offer.get("priceSpecification"), dict):
                price = offer["priceSpecification"].get("price")
                currency = currency or offer["priceSpecification"].get("priceCurrency")
            if price is None:
                continue
            item_price = _decimal(price)
            shipping = cls._shipping_cost(offer)
            seller = offer.get("seller")
            if isinstance(seller, dict):
                seller = seller.get("name")
            listing_url = str(offer.get("url") or page_url)
            listing_id = str(offer.get("sku") or product.get("sku") or _payload_hash([page_url, index, offer])[:24])
            normalized.append(
                Offer(
                    source=cls.name,
                    source_listing_id=listing_id,
                    title=str(product.get("name") or "Untitled product"),
                    url=listing_url,
                    condition=_first_text(offer.get("itemCondition")),
                    seller=_first_text(seller),
                    item_price=item_price,
                    shipping_cost=shipping,
                    unknown_cost_components=[] if shipping is not None else ["shipping"],
                    total_cost=item_price + (shipping or Decimal("0")),
                    currency=str(currency or "EUR"),
                    availability=_first_text(offer.get("availability")),
                    raw_source_hash=_payload_hash({"product": product, "offer": offer}),
                    brand=_first_text(brand),
                    gtin=_first_text(product.get("gtin") or product.get("gtin13") or product.get("gtin14") or product.get("gtin12")),
                    mpn=_first_text(product.get("mpn")),
                    model=_first_text(product.get("model")),
                )
            )
        return normalized

    @classmethod
    def _normalize_opengraph(cls, meta: dict[str, str], page_url: str) -> Offer | None:
        price = meta.get("product:price:amount") or meta.get("og:price:amount")
        if price is None:
            return None
        currency = meta.get("product:price:currency") or meta.get("og:price:currency") or "EUR"
        title = meta.get("og:title") or meta.get("twitter:title") or "Untitled product"
        listing_url = meta.get("og:url") or page_url
        payload = {"meta": meta, "page_url": page_url}
        item_price = _decimal(price)
        return Offer(
            source=cls.name,
            source_listing_id=_payload_hash(listing_url)[:24],
            title=title,
            url=listing_url,
            condition=_first_text(meta.get("product:condition")),
            item_price=item_price,
            shipping_cost=None,
            unknown_cost_components=["shipping"],
            total_cost=item_price,
            currency=currency,
            availability=_first_text(meta.get("product:availability")),
            raw_source_hash=_payload_hash(payload),
        )

    @staticmethod
    def _shipping_cost(offer: dict) -> Decimal | None:
        details = offer.get("shippingDetails")
        if not isinstance(details, dict):
            return None
        rate = details.get("shippingRate")
        if isinstance(rate, dict):
            if rate.get("value") is not None:
                return _decimal(rate.get("value"))
            nested = rate.get("shippingRate")
            if isinstance(nested, dict):
                return _decimal(nested.get("value"))
            if nested is not None:
                return _decimal(nested)
        if rate is not None:
            return _decimal(rate)
        return None
