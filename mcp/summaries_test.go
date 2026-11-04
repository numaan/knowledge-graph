package main

import (
	"reflect"
	"testing"
)

func TestSummaryTargetsNearestFirst(t *testing.T) {
	got := summaryTargets("kgt", "/app/scenarios/store.py")
	want := []string{"kgt:app/scenarios/store.py", "kgt:folder:app/scenarios/store.py", "kgt:folder:app/scenarios",
		"kgt:folder:app", "kgt:folder:/", "kgt"}
	if !reflect.DeepEqual(got, want) {
		t.Fatalf("file path:\n got %v\nwant %v", got, want)
	}
	if got := summaryTargets("kgt", `main.go`); !reflect.DeepEqual(got, []string{"kgt:main.go", "kgt:folder:main.go", "kgt:folder:/", "kgt"}) {
		t.Errorf("root file: %v", got)
	}
	if got := summaryTargets("kgt", ""); !reflect.DeepEqual(got, []string{"kgt:folder:/", "kgt"}) {
		t.Errorf("empty path: %v", got)
	}
	if got := folderTargets("kgt", "app/"); !reflect.DeepEqual(got, []string{"kgt:folder:app", "kgt:folder:/", "kgt"}) {
		t.Errorf("folder: %v", got)
	}
}

func TestDropEmptyStale(t *testing.T) {
	rows := []map[string]any{
		{"id": "a", "stale": nil, "stale_reason": nil},
		{"id": "b", "stale": true, "stale_reason": "code changed"},
		{"id": "c", "stale": false},
	}
	dropEmptyStale(rows)
	if _, ok := rows[0]["stale"]; ok || len(rows[0]) != 1 || len(rows[2]) != 1 {
		t.Errorf("empty stale fields kept: %v", rows)
	}
	if rows[1]["stale"] != true || rows[1]["stale_reason"] != "code changed" {
		t.Errorf("stale node lost its flag: %v", rows[1])
	}
}
