from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed

from .connectors import ShoppingConnector
from .models import ConnectorResult, SearchRequest, SearchResponse
from .resolver import ProductResolver
from .storage import ShoppingStore


class ShoppingService:
    def __init__(self, store: ShoppingStore, connectors: list[ShoppingConnector]) -> None:
        self.store = store
        self.connectors = connectors

    def search(self, request: SearchRequest) -> SearchResponse:
        connector_results = self._search_connectors(request)
        offers = [offer for result in connector_results for offer in result.offers]
        resolver = ProductResolver(self.store.list_products())
        touched_products = {}
        observations_written = 0
        for offer in offers:
            product = resolver.resolve(offer)
            touched_products[product.id] = product
            self.store.upsert_product(product)
            self.store.persist_offer(offer)
            observations_written += 1
        return SearchResponse(
            query=request.query,
            products=list(touched_products.values()),
            offers=offers,
            connector_results=connector_results,
            observations_written=observations_written,
        )

    def _search_connectors(self, request: SearchRequest) -> list[ConnectorResult]:
        if not self.connectors:
            return []
        results: dict[str, ConnectorResult] = {}
        with ThreadPoolExecutor(max_workers=min(4, len(self.connectors))) as pool:
            futures = {pool.submit(connector.search, request): connector for connector in self.connectors}
            for future in as_completed(futures):
                connector = futures[future]
                try:
                    offers = future.result()
                    results[connector.name] = ConnectorResult(connector=connector.name, offers=offers)
                except Exception as exc:
                    results[connector.name] = ConnectorResult(
                        connector=connector.name,
                        error=f"{type(exc).__name__}: {exc}",
                    )
        return [results[connector.name] for connector in self.connectors]
