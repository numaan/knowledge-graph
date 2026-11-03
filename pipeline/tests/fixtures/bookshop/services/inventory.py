"""Stock levels of books in the warehouse."""
from models import Book


class OutOfStock(Exception):
    pass


def reserve_stock(book: Book, quantity: int) -> None:
    """Hold books for an order; fails when the warehouse has too few."""
    if book.stock < quantity:
        raise OutOfStock(book.isbn)
    book.stock -= quantity


def release_stock(book: Book, quantity: int) -> None:
    book.stock += quantity


def restock_book(book: Book, quantity: int) -> None:
    """Add delivered copies from the supplier to the stock."""
    book.stock += quantity


def low_stock_report(books: list[Book], threshold: int = 3) -> list[str]:
    """ISBNs of books that need a new supplier delivery."""
    return [b.isbn for b in books if b.stock <= threshold]
