import os

import httpx

BILLING_URL = os.environ.get("BILLING_URL", "http://billing:3000")


def create_invoice(order):
    return httpx.post(f"{BILLING_URL}/invoices", json={"orderId": order.id}).json()
