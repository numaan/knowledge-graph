"""Checkout: place, cancel and refund orders."""
from models import Order
from services.email import send_receipt
from services.inventory import release_stock, reserve_stock
from services.pricing import order_total


def place_order(order: Order) -> int:
    """Reserve the stock of every line, charge the total and email a receipt."""
    for book, quantity in order.lines:
        reserve_stock(book, quantity)
    total = order_total(order)
    order.status = "paid"
    send_receipt(order.customer.email, order.id, total)
    return total


def cancel_order(order: Order) -> None:
    """Cancel an order that is not shipped yet and put the books back in stock."""
    if order.status == "shipped":
        raise ValueError("a shipped order cannot be cancelled")
    for book, quantity in order.lines:
        release_stock(book, quantity)
    order.status = "cancelled"


def refund_order(order: Order) -> int:
    """Give the money back for a cancelled order."""
    if order.status != "cancelled":
        raise ValueError("cancel the order before the refund")
    order.status = "refunded"
    return order_total(order)


def ship_order(order: Order) -> None:
    order.status = "shipped"
