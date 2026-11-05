package main

import "testing"

func chunk(start, end int, text string) map[string]any {
	return map[string]any{"start_line": int64(start), "end_line": int64(end), "text": text}
}

func TestJoinChunks(t *testing.T) {
	// enum Rule: member chunk (header and constants), constructor, method, closing brace in the member chunk
	got := joinChunks([]any{
		chunk(1, 4, "public enum Rule {\n    A(1),\n    B(2);\n    private final int id;"),
		chunk(5, 5, "    Rule(int id) { this.id = id; }"),
		chunk(6, 8, "    public int getId() {\n        return id;\n    }"),
		chunk(9, 9, "}"),
	})
	want := "public enum Rule {\n    A(1),\n    B(2);\n    private final int id;\n    Rule(int id) { this.id = id; }\n" +
		"    public int getId() {\n        return id;\n    }\n}\n"
	if got != want {
		t.Fatalf("joinChunks:\n%q\nwant\n%q", got, want)
	}

	// a nested type inside a class without methods: its chunk lies inside the class chunk and is skipped
	got = joinChunks([]any{chunk(1, 4, "class C {\n  enum E { X }\n  int f;\n}"), chunk(2, 2, "  enum E { X }")})
	if got != "class C {\n  enum E { X }\n  int f;\n}\n" {
		t.Fatalf("nested chunk repeated: %q", got)
	}

	// partial overlap: the lines already taken are dropped
	got = joinChunks([]any{chunk(1, 2, "a\nb"), chunk(2, 3, "b\nc")})
	if got != "a\nb\nc\n" {
		t.Fatalf("overlap: %q", got)
	}

	if joinChunks(nil) != "" {
		t.Fatal("no chunks should give empty code")
	}
}
