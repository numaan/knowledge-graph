"""Outgoing customer emails."""
OUTBOX: list[tuple[str, str]] = []


def send_receipt(email: str, order_id: str, total_cents: int) -> None:
    """Email the receipt with the amount paid."""
    OUTBOX.append((email, f"Receipt for order {order_id}: {total_cents / 100:.2f}"))


def send_password_reset(email: str, token: str) -> None:
    OUTBOX.append((email, f"Reset your password: https://shop.example/reset?token={token}"))
