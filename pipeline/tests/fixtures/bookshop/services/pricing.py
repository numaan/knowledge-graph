"""Prices, discount codes, sales tax and loyalty points."""
from models import Order

TAX_RATE = 0.2
DISCOUNTS = {"WELCOME10": 0.10, "SUMMER25": 0.25}


def apply_discount(subtotal: int, code: str | None) -> int:
    """Take a percentage off the subtotal for a valid discount code."""
    return round(subtotal * (1 - DISCOUNTS.get(code or "", 0.0)))


def compute_tax(amount: int) -> int:
    """Sales tax (VAT) on an amount in cents."""
    return round(amount * TAX_RATE)


def order_total(order: Order) -> int:
    """Subtotal of all lines, minus the discount, plus tax."""
    subtotal = sum(book.price_cents * quantity for book, quantity in order.lines)
    net = apply_discount(subtotal, order.discount_code)
    return net + compute_tax(net)


def award_loyalty_points(order: Order) -> int:
    """One loyalty point per full 100 cents of the total."""
    points = order_total(order) // 100
    order.customer.loyalty_points += points
    return points
