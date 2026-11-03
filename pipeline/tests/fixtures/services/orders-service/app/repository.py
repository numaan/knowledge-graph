from app.models import Order


def load_order(db, order_id: int):
    return db.execute("SELECT id, status FROM orders WHERE id = :id", {"id": order_id}).first()


def save_order(db, body: dict):
    order = Order(customer_id=body["customer_id"], status="new")
    db.add(order)
    for line in body.get("lines", []):
        add_line(db, order, line)
    return order


def add_line(db, order, line: dict):
    db.execute("INSERT INTO order_lines (order_id, sku) VALUES (:o, :s)", {"o": order.id, "s": line["sku"]})
