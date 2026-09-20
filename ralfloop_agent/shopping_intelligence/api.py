from __future__ import annotations

import os
from pathlib import Path

from fastapi import FastAPI, HTTPException

from .connectors import EbayConnector, StructuredDataConnector
from .models import HistoryPoint, SearchRequest, SearchResponse
from .service import ShoppingService
from .storage import ShoppingStore


def create_app(*, db_path: str | None = None, service: ShoppingService | None = None) -> FastAPI:
    app = FastAPI(title="Bot-tazzi Shopping Intelligence", version="0.1.0")
    if service is None:
        path = db_path or os.getenv("SHOPPING_DB_PATH", "var/shopping-intelligence.sqlite")
        store = ShoppingStore(str(Path(path)))
        ebay = EbayConnector()
        structured = StructuredDataConnector()
        service = ShoppingService(store, [ebay, structured])
        app.state.ebay_configured = ebay.configured
    else:
        app.state.ebay_configured = None
    app.state.shopping_service = service

    @app.get("/health")
    def health() -> dict:
        return {
            "ok": True,
            "connectors": [connector.name for connector in service.connectors],
            "ebay_configured": app.state.ebay_configured,
        }

    @app.post("/search", response_model=SearchResponse)
    def search(request: SearchRequest) -> SearchResponse:
        return service.search(request)

    @app.get("/products/{product_id}/history", response_model=list[HistoryPoint])
    def history(product_id: str) -> list[HistoryPoint]:
        points = service.store.history(product_id)
        if not points:
            raise HTTPException(status_code=404, detail="product history not found")
        return points

    return app


app = create_app()
