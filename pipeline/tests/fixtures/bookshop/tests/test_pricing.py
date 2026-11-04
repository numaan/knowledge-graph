from services.pricing import apply_discount, compute_tax


def test_discount_code():
    assert apply_discount(1000, "WELCOME10") == 900


def test_tax():
    assert compute_tax(1000) == 200
