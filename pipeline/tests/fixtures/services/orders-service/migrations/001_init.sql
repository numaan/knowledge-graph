-- orders and their lines
CREATE TABLE orders (
    id SERIAL PRIMARY KEY,
    customer_id INTEGER NOT NULL,
    created_at TIMESTAMP DEFAULT now()
);

CREATE TABLE IF NOT EXISTS order_lines (
    id SERIAL PRIMARY KEY,
    order_id INTEGER NOT NULL REFERENCES orders(id),
    sku VARCHAR(40) NOT NULL,
    CONSTRAINT uq_line UNIQUE (order_id, sku)
);
