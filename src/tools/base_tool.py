"""Base utilities for tool implementations.

Provides a simulated backend client for demo purposes.
In production, these would be replaced with real API calls
to CRM, calendar, order management, and ticketing systems.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

logger = logging.getLogger(__name__)


class MockBackendClient:
    """Simulated backend for demo purposes.

    In production, replace with actual HTTP clients calling
    your CRM, ERP, calendar, and ticketing APIs.
    """

    # Simulated customer database
    CUSTOMERS = {
        "C-1001": {
            "id": "C-1001",
            "name": "Maria Schmidt",
            "email": "maria.schmidt@example.com",
            "phone": "+49 170 1234567",
            "tier": "premium",
        },
        "C-1002": {
            "id": "C-1002",
            "name": "Thomas Müller",
            "email": "thomas.mueller@example.com",
            "phone": "+49 171 9876543",
            "tier": "standard",
        },
    }

    # Simulated orders
    ORDERS = {
        "ORD-5001": {
            "id": "ORD-5001",
            "customer_id": "C-1001",
            "status": "in_transit",
            "items": ["Laptop Stand", "USB-C Hub"],
            "estimated_delivery": (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d"),
            "delivery_window": "10:00-14:00",
        },
        "ORD-5002": {
            "id": "ORD-5002",
            "customer_id": "C-1001",
            "status": "delivered",
            "items": ["Wireless Mouse"],
            "delivered_at": (datetime.now() - timedelta(days=2)).strftime("%Y-%m-%d"),
        },
        "ORD-5003": {
            "id": "ORD-5003",
            "customer_id": "C-1002",
            "status": "processing",
            "items": ["Monitor", "HDMI Cable"],
            "estimated_delivery": (datetime.now() + timedelta(days=3)).strftime("%Y-%m-%d"),
        },
    }

    # Simulated calendar slots
    @staticmethod
    def get_available_slots(date: str) -> list[dict[str, str]]:
        """Return mock available appointment slots for a given date."""
        return [
            {"time": "09:00", "duration": "30min", "available": True},
            {"time": "10:00", "duration": "30min", "available": True},
            {"time": "11:00", "duration": "30min", "available": False},
            {"time": "14:00", "duration": "30min", "available": True},
            {"time": "15:30", "duration": "30min", "available": True},
        ]

    # Ticket counter for generating IDs
    _ticket_counter = 7000

    @classmethod
    def next_ticket_id(cls) -> str:
        cls._ticket_counter += 1
        return f"TKT-{cls._ticket_counter}"
