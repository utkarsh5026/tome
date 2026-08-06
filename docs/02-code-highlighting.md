# Code Highlighting Tour

One fenced block per language `tome`'s tokenizer knows about — a quick way to
eyeball the highlighter after a change to `LANG_SPECS` or the keyword tables.

## Rust

```rust
use std::collections::HashMap;

struct TokenBucket {
    capacity: u32,
    tokens: u32,
}

impl TokenBucket {
    fn try_take(&mut self, n: u32) -> bool {
        if self.tokens >= n {
            self.tokens -= n;
            true
        } else {
            false
        }
    }
}
```

## Python

```python
from dataclasses import dataclass

@dataclass
class TokenBucket:
    capacity: int
    tokens: int

    def try_take(self, n: int) -> bool:
        if self.tokens >= n:
            self.tokens -= n
            return True
        return False
```

## Bash

```bash
#!/usr/bin/env bash
set -euo pipefail

for port in 7979 7980 7981; do
    if ! curl -fs "http://127.0.0.1:${port}/api/version" >/dev/null; then
        echo "warning: nothing listening on ${port}" >&2
    fi
done
```

## SQL

```sql
SELECT d.rel, d.kind, g.title
FROM docs AS d
JOIN groups AS g ON g.gid = d.group
WHERE d.kind != 'other'
ORDER BY g.num, d.rel;
```

## TypeScript

```typescript
interface Doc {
  rel: string;
  title: string;
  label: string;
  kind: "README" | "SPEC" | "doc" | "other";
}

async function loadTree(): Promise<Doc[]> {
  const r = await fetch("/api/tree");
  return r.json();
}
```

## JavaScript

```javascript
function debounce(fn, ms) {
  let t = null;
  return (...args) => {
    clearTimeout(t);
    t = setTimeout(() => fn(...args), ms);
  };
}
```

## Go

```go
package main

import "net/http"

func main() {
    mux := http.NewServeMux()
    mux.HandleFunc("/api/version", versionHandler)
    http.ListenAndServe("127.0.0.1:7979", mux)
}
```

## JSON

```json
{
  "title": "my-repo",
  "icon": "📖",
  "pinned": ["README", "SPEC", "CONCEPTS"],
  "groupDirs": ["projects", "packages", "crates"]
}
```

## TOML

```toml
[project]
name = "tome-docs"
requires-python = ">=3.9"
dependencies = []

[tool.hatch.version]
path = "tome.py"
```

## YAML

```yaml
name: CI
on: [push, pull_request]
jobs:
  test:
    strategy:
      matrix:
        python: ["3.9", "3.13"]
        os: [ubuntu-latest, macos-latest, windows-latest]
```

## Dockerfile

```dockerfile
FROM python:3.13-slim
COPY tome.py /usr/local/bin/tome.py
ENTRYPOINT ["python3", "/usr/local/bin/tome.py"]
```

## Makefile

```makefile
test:
	python3 -m unittest

lint:
	uvx ruff check .
```

## Plain text

```text
No lang tag on this fence means no keyword table — just escaped text
in a monospace block. Still useful for pasted output or logs.
```

## File-reference form

A fence whose info string looks like `START:END:path` — the language comes
from the referenced file's extension, and the whole string becomes the label
tome shows above the block:

```1:9:tome.py
#!/usr/bin/env python3
"""tome — read any repo's markdown in a browser tab.

A zero-dependency local web reader for every markdown file in a repository, so
you can keep the docs open in one browser tab and code in the other window
instead of juggling editor splits.

  * DISCOVERS  ← every `*.md` under the repo, grouped the way the repo is laid
                 out (monorepo packages become their own sections).
```
