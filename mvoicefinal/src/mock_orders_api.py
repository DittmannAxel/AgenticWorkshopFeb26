from __future__ import annotations

import argparse
import json
import logging
import os
import time
from pathlib import Path

from aiohttp import web


_LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=_LOG_LEVEL,
    format="%(asctime)s:%(name)s:%(levelname)s:%(message)s",
)
logger = logging.getLogger("mock_orders_api")


def _load_data(path: Path) -> dict:
    if not path.exists():
        return {"customers": [], "orders": []}
    return json.loads(path.read_text(encoding="utf-8"))


def _customers_by_id(data: dict) -> dict[str, dict]:
    customers = data.get("customers") or []
    if not isinstance(customers, list):
        return {}
    out: dict[str, dict] = {}
    for c in customers:
        if isinstance(c, dict) and c.get("id"):
            out[str(c["id"])] = c
    return out


def _orders(data: dict) -> list[dict]:
    orders = data.get("orders") or []
    return [o for o in orders if isinstance(o, dict)]

def _norm(s: str) -> str:
    return " ".join((s or "").strip().lower().split())


@web.middleware
async def request_log_middleware(request: web.Request, handler):
    start = time.perf_counter()
    resp: web.StreamResponse | None = None
    try:
        resp = await handler(request)
        return resp
    finally:
        ms = int((time.perf_counter() - start) * 1000)
        status = getattr(resp, "status", "ERR")
        logger.info("%s %s -> %s (%dms)", request.method, request.rel_url, status, ms)


async def health(_request: web.Request) -> web.Response:
    return web.json_response({"status": "ok"})


async def get_order(request: web.Request) -> web.Response:
    data_path: Path = request.app["data_path"]
    data = _load_data(data_path)
    customers = _customers_by_id(data)

    order_id = request.match_info["order_id"].strip().upper()
    logger.info("lookup order_id=%s data=%s", order_id, data_path)
    for o in _orders(data):
        if str(o.get("id") or "").strip().upper() != order_id:
            continue
        customer_id = str(o.get("customer_id") or "").strip()
        customer = customers.get(customer_id, {})
        result = dict(o)
        if customer:
            result["customer_name"] = customer.get("name")
        logger.info("found order_id=%s", order_id)
        return web.json_response(result)

    if not order_id:
        return web.json_response({"error": "not found"}, status=404)
    logger.info("not found order_id=%s", order_id)
    return web.json_response({"error": "not found"}, status=404)


async def list_orders(request: web.Request) -> web.Response:
    data_path: Path = request.app["data_path"]
    data = _load_data(data_path)
    customers = _customers_by_id(data)

    customer_name = (request.query.get("customer_name") or "").strip()
    if not customer_name:
        # Return all orders (used for debugging / demo listing).
        results = []
        for o in _orders(data):
            row = dict(o)
            customer_id = str(row.get("customer_id") or "").strip()
            customer = customers.get(customer_id, {})
            if customer:
                row["customer_name"] = customer.get("name")
            results.append(row)
        logger.info("list all orders -> %d", len(results))
        return web.json_response({"orders": results})

    # Find matching customer by name or alias.
    customer_id = None
    customer_display = None
    for c in customers.values():
        if _norm(str(c.get("name") or "")) == _norm(customer_name):
            customer_id = str(c.get("id") or "").strip()
            customer_display = str(c.get("name") or "").strip()
            break
        aliases = c.get("aliases") or []
        if isinstance(aliases, list) and any(_norm(str(a)) == _norm(customer_name) for a in aliases):
            customer_id = str(c.get("id") or "").strip()
            customer_display = str(c.get("name") or "").strip()
            break
    if not customer_id:
        logger.info("list orders customer_name=%s -> 0 (no customer match)", customer_name)
        return web.json_response({"orders": []})

    results = []
    for o in _orders(data):
        if str(o.get("customer_id") or "").strip() != customer_id:
            continue
        row = dict(o)
        row["customer_name"] = customer_display or customer_name
        results.append(row)

    logger.info("list orders customer_name=%s customer_id=%s -> %d", customer_name, customer_id, len(results))
    return web.json_response({"orders": results})


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8083)
    parser.add_argument(
        "--data",
        type=str,
        default=str(Path(__file__).resolve().parents[1] / "kundendaten.json"),
        help="Path to kundendaten.json",
    )
    args = parser.parse_args()

    app = web.Application(middlewares=[request_log_middleware])
    app["data_path"] = Path(args.data)
    app.router.add_get("/health", health)
    app.router.add_get("/orders/{order_id}", get_order)
    app.router.add_get("/orders", list_orders)

    logger.info("mock orders api listening on :%s using data=%s", args.port, args.data)
    web.run_app(app, port=args.port)


if __name__ == "__main__":
    main()
