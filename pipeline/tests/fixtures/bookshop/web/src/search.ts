// Book search box: find books by title or author as the user types.
export async function searchBooks(query: string): Promise<string[]> {
  const res = await fetch(`/api/books?q=${encodeURIComponent(query)}`);
  return (await res.json()).titles;
}
