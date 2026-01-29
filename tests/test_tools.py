"""Tests for customer service tools."""

from src.tools.crm_tool import identify_customer, get_customer_details
from src.tools.calendar_tool import check_availability, book_appointment
from src.tools.order_tool import get_recent_orders, get_order_status
from src.tools.ticket_tool import create_ticket, get_ticket_status


class TestCrmTool:
    def test_identify_known_customer(self):
        result = identify_customer(phone="+49 170 1234567")
        assert result["found"] is True
        assert result["customer_id"] == "C-1001"
        assert result["name"] == "Maria Schmidt"

    def test_identify_unknown_customer(self):
        result = identify_customer(phone="+49 999 0000000")
        assert result["found"] is False

    def test_get_customer_details(self):
        result = get_customer_details(customer_id="C-1001")
        assert result["name"] == "Maria Schmidt"
        assert result["tier"] == "premium"

    def test_get_customer_details_not_found(self):
        result = get_customer_details(customer_id="C-9999")
        assert "error" in result


class TestCalendarTool:
    def test_check_availability(self):
        result = check_availability(date="2025-02-03")
        assert result["date"] == "2025-02-03"
        assert result["total_available"] > 0
        assert all(s["available"] for s in result["available_slots"])

    def test_book_appointment(self):
        result = book_appointment(
            date="2025-02-03",
            time="10:00",
            customer_id="C-1001",
            reason="Beratungsgespräch",
        )
        assert result["success"] is True
        assert "appointment_id" in result
        assert result["customer_id"] == "C-1001"


class TestOrderTool:
    def test_get_recent_orders(self):
        result = get_recent_orders(customer_id="C-1001")
        assert result["total_orders"] >= 1
        assert all(o["customer_id"] == "C-1001" for o in result["orders"])

    def test_get_recent_orders_no_orders(self):
        result = get_recent_orders(customer_id="C-9999")
        assert result["total_orders"] == 0

    def test_get_order_status(self):
        result = get_order_status(order_id="ORD-5001")
        assert result["status"] == "in_transit"

    def test_get_order_status_not_found(self):
        result = get_order_status(order_id="ORD-9999")
        assert "error" in result


class TestTicketTool:
    def test_create_ticket(self):
        result = create_ticket(
            customer_id="C-1001",
            category="damaged_delivery",
            priority="high",
            description="Laptop Stand arrived with broken hinge.",
        )
        assert result["success"] is True
        assert result["ticket_id"].startswith("TKT-")
        assert result["priority"] == "high"
        assert result["expected_response_hours"] == 24

    def test_create_ticket_urgent(self):
        result = create_ticket(
            customer_id="C-1001",
            category="missing_item",
            priority="urgent",
            description="Order arrived empty.",
        )
        assert result["expected_response_hours"] == 4

    def test_get_ticket_status(self):
        result = get_ticket_status(ticket_id="TKT-7001")
        assert result["status"] == "open"
