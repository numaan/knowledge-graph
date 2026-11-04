export function SearchForm({ q = "", type = "", autoFocus = false }: { q?: string; type?: string; autoFocus?: boolean }) {
  return (
    <form action="/search" role="search" className="search-form">
      <label htmlFor="q" className="skip">Search the knowledge base</label>
      <input id="q" type="search" name="q" defaultValue={q} autoFocus={autoFocus}
        placeholder="e.g. who can invite users in bulk" required />
      {type && <input type="hidden" name="type" value={type} />}
      <button type="submit" className="primary">Search</button>
    </form>
  );
}
