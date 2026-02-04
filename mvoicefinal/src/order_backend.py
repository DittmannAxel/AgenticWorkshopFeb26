from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional, Protocol

import aiohttp

from src.set_logging import logger


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

    async def list_orders(self) -> list[dict[str, Any]]:
        ...


def _norm(s: str) -> str:
    return " ".join((s or "").strip().lower().split())


class CustomerDataError(RuntimeError):
    pass


class JsonFileOrderBackend:
    """File-based backend reading orders/customers from a JSON file.

    This lets you change `kundendaten.json` without changing any code.
    """

    def __init__(self, json_path: str | Path):
        self._path = Path(json_path)

    def _load(self) -> dict[str, Any]:
        if not self._path.exists():
            raise CustomerDataError(f"Customer data file not found: {self._path}")
        try:
            return json.loads(self._path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            raise CustomerDataError(f"Invalid JSON in {self._path}: {e}") from e

    def _customers_index(self, data: dict[str, Any]) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
        customers = data.get("customers")
        if customers is None:
            customers = []
        if not isinstance(customers, list):
            raise CustomerDataError("`customers` must be a list")

        by_id: dict[str, dict[str, Any]] = {}
        by_name: dict[str, dict[str, Any]] = {}
        for c in customers:
            if not isinstance(c, dict):
                continue
            cid = str(c.get("id") or "").strip()
            name = str(c.get("name") or "").strip()
            if cid:
                by_id[cid] = c
            if name:
                by_name[_norm(name)] = c
            aliases = c.get("aliases") or []
            if isinstance(aliases, list):
                for a in aliases:
                    if isinstance(a, str) and a.strip():
                        by_name[_norm(a)] = c
        return by_id, by_name

    def _orders(self, data: dict[str, Any]) -> list[dict[str, Any]]:
        orders = data.get("orders")
        if orders is None:
            return []
        if not isinstance(orders, list):
            raise CustomerDataError("`orders` must be a list")
        return [o for o in orders if isinstance(o, dict)]

    async def get_order_status(self, order_id: str) -> dict[str, Any]:
        data = self._load()
        customers_by_id, _ = self._customers_index(data)

        order_id_norm = (order_id or "").strip().upper()
        for o in self._orders(data):
            if str(o.get("id") or "").strip().upper() != order_id_norm:
                continue
            customer_id = str(o.get("customer_id") or "").strip()
            customer = customers_by_id.get(customer_id, {})
            result = dict(o)
            if customer_id:
                result["customer_id"] = customer_id
            if customer:
                result["customer_name"] = customer.get("name")
            return {"found": True, **result}

        return {"found": False, "error": f"Order {order_id} not found."}

    async def find_recent_orders_by_customer_name(self, customer_name: str) -> list[dict[str, Any]]:
        data = self._load()
        customers_by_id, customers_by_name = self._customers_index(data)

        customer = customers_by_name.get(_norm(customer_name))
        if not customer:
            return []

        customer_id = str(customer.get("id") or "").strip()
        if not customer_id:
            return []

        name = str(customer.get("name") or "").strip()
        results: list[dict[str, Any]] = []
        for o in self._orders(data):
            if str(o.get("customer_id") or "").strip() != customer_id:
                continue
            row = dict(o)
            row["customer_name"] = name
            results.append(row)
        return results

    async def list_orders(self) -> list[dict[str, Any]]:
        data = self._load()
        customers_by_id, _ = self._customers_index(data)

        results: list[dict[str, Any]] = []
        for o in self._orders(data):
            row = dict(o)
            customer_id = str(row.get("customer_id") or "").strip()
            if customer_id:
                customer = customers_by_id.get(customer_id, {})
                if isinstance(customer, dict) and customer.get("name"):
                    row["customer_name"] = customer.get("name")
            results.append(row)
        return results


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
        logger.info("Orders API call: GET %s", url)
        async with aiohttp.ClientSession(timeout=self._timeout) as session:
            async with session.get(url) as resp:
                data = await resp.json(content_type=None)
                if resp.status >= 400:
                    logger.warning("Orders API error: %s -> HTTP %s (%s)", url, resp.status, data)
                    return {"found": False, "error": data or f"HTTP {resp.status}"}
                return {"found": True, **(data or {})}

    async def find_recent_orders_by_customer_name(self, customer_name: str) -> list[dict[str, Any]]:
        url = f"{self._base_url}/orders"
        logger.info("Orders API call: GET %s?customer_name=%s", url, customer_name)
        async with aiohttp.ClientSession(timeout=self._timeout) as session:
            async with session.get(url, params={"customer_name": customer_name}) as resp:
                data = await resp.json(content_type=None)
                if resp.status >= 400:
                    logger.warning(
                        "Orders API error: %s?customer_name=%s -> HTTP %s (%s)",
                        url,
                        customer_name,
                        resp.status,
                        data,
                    )
                    return []
                if isinstance(data, dict) and "orders" in data and isinstance(data["orders"], list):
                    return data["orders"]
                if isinstance(data, list):
                    return data
                return []

    async def list_orders(self) -> list[dict[str, Any]]:
        url = f"{self._base_url}/orders"
        logger.info("Orders API call: GET %s", url)
        async with aiohttp.ClientSession(timeout=self._timeout) as session:
            async with session.get(url) as resp:
                data = await resp.json(content_type=None)
                if resp.status >= 400:
                    logger.warning("Orders API error: %s -> HTTP %s (%s)", url, resp.status, data)
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
