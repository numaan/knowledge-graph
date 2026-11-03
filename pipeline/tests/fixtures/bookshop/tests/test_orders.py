from api.orders import cancel_order, place_order, refund_order
from models import Book, Customer, Order


def _order():
    return Order("o1", Customer("a@example.com", "Ann", ""), [(Book("1", "Dune", 1000, 5), 2)])


def test_place_order_charges_total_with_tax():
    assert place_order(_order()) == 2400


def test_refund_needs_cancel_first():
    order = _order()
    place_order(order)
    cancel_order(order)
    assert refund_order(order) == 2400
