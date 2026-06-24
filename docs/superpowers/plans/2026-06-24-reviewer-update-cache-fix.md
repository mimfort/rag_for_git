# `reviewer update`: cache-control fix

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `reviewer update` detects new PyPI versions on the first run, not after 2+ retries.

**Architecture:** Single 4-line change in `reviewer/entrypoints/cli.py:484-486` — wrap the PyPI JSON URL in a `urllib.request.Request` with `Cache-Control: no-cache, no-store` and `Pragma: no-cache` headers to bypass Fastly CDN caching of stale version info.

**Tech Stack:** Python stdlib `urllib.request` (already used in the function).

---

### Task 1: Add Cache-Control headers to the update command

**Files:**
- Modify: `reviewer/entrypoints/cli.py:484-486`

- [ ] **Step 1: Replace the urllib call**

Change lines 484-486 from:

```python
    try:
        with urllib.request.urlopen("https://pypi.org/pypi/rag-reviewer/json", timeout=10) as resp:
            latest_ver = json.loads(resp.read())["info"]["version"]
```

To:

```python
    try:
        req = urllib.request.Request(
            "https://pypi.org/pypi/rag-reviewer/json",
            headers={"Cache-Control": "no-cache, no-store", "Pragma": "no-cache"},
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            latest_ver = json.loads(resp.read())["info"]["version"]
```

- [ ] **Step 2: Verify the function still parses correctly**

```bash
python -c "import ast; ast.parse(open('reviewer/entrypoints/cli.py').read()); print('OK')"
```

Expected: `OK`

- [ ] **Step 3: Commit**

```bash
git add reviewer/entrypoints/cli.py
git commit -m "fix: add Cache-Control headers to reviewer update PyPI request"
```
