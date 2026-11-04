package main

// collapseDuplicates keeps the best-ranked row of each duplicate group (rows that share "canonical",
// from DUPLICATE_OF edges) and lists the other copies under "also_in". Rows must already be ACL-filtered,
// so a copy that the server cannot see is never named. At most k rows are returned.
func collapseDuplicates(rows []map[string]any, k int) []map[string]any {
	out := []map[string]any{}
	first := map[any]map[string]any{}
	for _, r := range rows {
		group := r["canonical"]
		if group == nil {
			group = r["id"]
		}
		delete(r, "canonical")
		if kept, ok := first[group]; ok {
			also, _ := kept["also_in"].([]map[string]any)
			kept["also_in"] = append(also, map[string]any{"id": r["id"], "title": r["title"], "url": r["url"]})
			continue
		}
		if len(out) == k {
			continue // keep reading: a later copy of a kept row still goes to also_in
		}
		first[group] = r
		out = append(out, r)
	}
	return out
}
