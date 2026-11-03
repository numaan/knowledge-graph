from dataclasses import dataclass, field


@dataclass
class Book:
    isbn: str
    title: str
    price_cents: int
    stock: int = 0


@dataclass
class Customer:
    email: str
    name: str
    password_hash: str
    loyalty_points: int = 0


@dataclass
class Order:
    id: str
    customer: Customer
    lines: list = field(default_factory=list)
    status: str = "open"
    discount_code: str | None = None
