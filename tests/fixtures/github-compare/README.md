Recorded `GET /repos/{owner}/{repo}/compare/{deployed}...{merge}` responses
(2026-09-20, trimmed to the fields `ancestry_from_compare` reads). The URL is
always `compare/<deployed sha>...<merge sha>`:

| file | deployed | merge | git truth |
|---|---|---|---|
| `deployed_contains_merge.json` | `fb9fe6b` | `378a576` (its parent) | merge IS an ancestor |
| `deployed_is_merge.json` | `fb9fe6b` | `fb9fe6b` | same commit: contained |
| `deployed_predates_merge.json` | `378a576` | `fb9fe6b` (its child) | NOT contained |
| `diverged.json` | `378a576` | `7762df9` (a side branch) | NOT contained |

An unknown sha answers 404 with no body of this shape.
