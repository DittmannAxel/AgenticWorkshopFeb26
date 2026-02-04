from __future__ import annotations

import argparse

from aiohttp import web


MOCK_ORDERS = {
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


async def health(_request: web.Request) -> web.Response:
    return web.json_response({"status": "ok"})


async def get_order(request: web.Request) -> web.Response:
    order_id = request.match_info["order_id"].upper()
    order = MOCK_ORDERS.get(order_id)
    if not order:
        return web.json_response({"error": "not found"}, status=404)
    return web.json_response(order)


async def list_orders(request: web.Request) -> web.Response:
    customer_name = (request.query.get("customer_name") or "").strip()
    if not customer_name:
        return web.json_response({"orders": []})
    orders = [
        o for o in MOCK_ORDERS.values()
        if o.get("customer_name", "").lower() == customer_name.lower()
    ]
    return web.json_response({"orders": orders})


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8083)
    args = parser.parse_args()

    app = web.Application()
    app.router.add_get("/health", health)
    app.router.add_get("/orders/{order_id}", get_order)
    app.router.add_get("/orders", list_orders)

    web.run_app(app, port=args.port)


if __name__ == "__main__":
    main()

