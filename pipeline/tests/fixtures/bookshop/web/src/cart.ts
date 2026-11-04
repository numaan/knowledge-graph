// Shopping cart in the browser; checkout posts the cart to the orders API.
export interface CartLine { isbn: string; quantity: number }

export class Cart {
  lines: CartLine[] = [];

  addToCart(isbn: string, quantity = 1): void {
    const line = this.lines.find((l) => l.isbn === isbn);
    if (line) line.quantity += quantity;
    else this.lines.push({ isbn, quantity });
  }

  removeFromCart(isbn: string): void {
    this.lines = this.lines.filter((l) => l.isbn !== isbn);
  }

  async checkout(discountCode?: string): Promise<number> {
    const res = await fetch("/api/orders", { method: "POST", body: JSON.stringify({ lines: this.lines, discountCode }) });
    this.lines = [];
    return (await res.json()).total;
  }
}
