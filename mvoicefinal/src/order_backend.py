from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Optional, Protocol

import aiohttp


@dataclass(frozen=True)
class OrderStatus:
    order_id: str
    status: str
    estimated_delivery: Optional[str] = None
    delivery_window: Optional[str] = None
    customer_name: Optional[str] = None


class OrderBackend(Protocol):
    async def get_order_status(self, order_id: str) -> dict[str, Any]:
        ...

    async def find_recent_orders_by_customer_name(self, customer_name: str) -> list[dict[str, Any]]:
        ...


class MockOrderBackend:
    """In-memory fallback backend (no external service required)."""

    _CUSTOMERS_BY_NAME = {
        "maria schmidt": {"customer_id": "C-1001", "name": "Maria Schmidt"},
        "thomas müller": {"customer_id": "C-1002", "name": "Thomas Müller"},
        "thomas mueller": {"customer_id": "C-1002", "name": "Thomas Müller"},
    }

    _ORDERS = {
        "ORD-5001": {
            "id": "ORD-5001",
            "customer_name": "Maria Schmidt",
            "status": "in_transit",
            "estimated_delivery": "tomorrow",
            "delivery_window": "10:00-14:00",
        },
        "ORD-5002": {
            "id": "ORD-5002",
            "customer_name": "Maria Schmidt",
            "status": "delivered",
        },
        "ORD-5003": {
            "id": "ORD-5003",
            "customer_name": "Thomas Müller",
            "status": "processing",
            "estimated_delivery": "in three days",
        },
    }

    async def get_order_status(self, order_id: str) -> dict[str, Any]:
        order = self._ORDERS.get(order_id.upper())
        if not order:
            return {"found": False, "error": f"Order {order_id} not found."}
        return {"found": True, **order}

    async def find_recent_orders_by_customer_name(self, customer_name: str) -> list[dict[str, Any]]:
        customer = self._CUSTOMERS_BY_NAME.get(customer_name.strip().lower())
        if not customer:
            return []
        name = customer["name"]
        return [o for o in self._ORDERS.values() if o.get("customer_name") == name]


class HttpOrderBackend:
    """HTTP backend.

    Expected endpoints:
    - GET {base_url}/orders/{order_id}
    - GET {base_url}/orders?customer_name=<name>
    """

    def __init__(self, base_url: str, timeout_s: float = 10.0):
        self._base_url = base_url.rstrip("/")
        self._timeout = aiohttp.ClientTimeout(total=timeout_s)

    async def get_order_status(self, order_id: str) -> dict[str, Any]:
        url = f"{self._base_url}/orders/{order_id}"
        async with aiohttp.ClientSession(timeout=self._timeout) as session:
            async with session.get(url) as resp:
                data = await resp.json(content_type=None)
                if resp.status >= 400:
                    return {"found": False, "error": data or f"HTTP {resp.status}"}
                return {"found": True, **(data or {})}

    async def find_recent_orders_by_customer_name(self, customer_name: str) -> list[dict[str, Any]]:
        url = f"{self._base_url}/orders"
        async with aiohttp.ClientSession(timeout=self._timeout) as session:
            async with session.get(url, params={"customer_name": customer_name}) as resp:
                data = await resp.json(content_type=None)
                if resp.status >= 400:
                    return []
                if isinstance(data, dict) and "orders" in data and isinstance(data["orders"], list):
                    return data["orders"]
                if isinstance(data, list):
                    return data
                return []


_ORDER_ID_RE = re.compile(r"\bORD[-\s]?\d{3,}\b", re.IGNORECASE)


def extract_order_id(text: str) -> Optional[str]:
    match = _ORDER_ID_RE.search(text or "")
    if not match:
        return None
    raw = match.group(0).upper().replace(" ", "").replace("ORD", "ORD-")
    raw = raw.replace("ORD--", "ORD-")
    return raw

