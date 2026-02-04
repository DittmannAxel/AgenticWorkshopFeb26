from __future__ import annotations

import argparse
import json
from pathlib import Path

from aiohttp import web


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


async def health(_request: web.Request) -> web.Response:
    return web.json_response({"status": "ok"})


async def get_order(request: web.Request) -> web.Response:
    data_path: Path = request.app["data_path"]
    data = _load_data(data_path)
    customers = _customers_by_id(data)

    order_id = request.match_info["order_id"].strip().upper()
    for o in _orders(data):
        if str(o.get("id") or "").strip().upper() != order_id:
            continue
        customer_id = str(o.get("customer_id") or "").strip()
        customer = customers.get(customer_id, {})
        result = dict(o)
        if customer:
            result["customer_name"] = customer.get("name")
        return web.json_response(result)

    if not order_id:
        return web.json_response({"error": "not found"}, status=404)
    return web.json_response({"error": "not found"}, status=404)


async def list_orders(request: web.Request) -> web.Response:
    data_path: Path = request.app["data_path"]
    data = _load_data(data_path)
    customers = _customers_by_id(data)

    customer_name = (request.query.get("customer_name") or "").strip()
    if not customer_name:
        return web.json_response({"orders": []})

    # Find matching customer by exact name match (simple demo behavior).
    customer_id = None
    for c in customers.values():
        if str(c.get("name") or "").strip().lower() == customer_name.lower():
            customer_id = str(c.get("id") or "").strip()
            break
    if not customer_id:
        return web.json_response({"orders": []})

    results = []
    for o in _orders(data):
        if str(o.get("customer_id") or "").strip() != customer_id:
            continue
        row = dict(o)
        row["customer_name"] = customer_name
        results.append(row)

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

    app = web.Application()
    app["data_path"] = Path(args.data)
    app.router.add_get("/health", health)
    app.router.add_get("/orders/{order_id}", get_order)
    app.router.add_get("/orders", list_orders)

    web.run_app(app, port=args.port)


if __name__ == "__main__":
    main()
