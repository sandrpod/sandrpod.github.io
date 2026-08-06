# Two ways to give a deepagents agent a sandbox you own

*Both work. Testing both is what found the bugs.*

---

[deepagents](https://github.com/langchain-ai/deepagents) hands your agent a
filesystem and a shell through a `backend` object. Swap the backend and the same
agent runs somewhere else — your process, a container, someone's cloud.

If that somewhere else should be **your** infrastructure, and you are running
[SandrPod](https://github.com/sandrpod/sandrpod) for it, there are two ways in:

- **`langchain-e2b`** — E2B's own deepagents backend. It wraps an `e2b.Sandbox`,
  and an `e2b.Sandbox` will talk to SandrPod when you point `E2B_DOMAIN` at it.
- **`langchain-sandrpod`** — SandrPod's own, against the native API.

This is a comparison of the two, run against a live deployment: the same twelve
backend assertions down each path, then the same agent given the same job three
times each. Both routes pass everything. What made the exercise worth doing is
what it turned up along the way — three defects, two of them silent, and one of
those was mine to fix rather than theirs.

Versions throughout: `deepagents` 0.6.12, `langchain-e2b` 0.0.6,
`langchain-sandrpod` 0.2.7, SandrPod v0.5.3 — the version these numbers were
measured against. v0.5.4 and v0.5.5 followed with resource-leak fixes, neither
of which changes any behaviour measured here.

---

## The two wirings

**Route 1 — through E2B's backend.** You build a normal `e2b.Sandbox`, which
lands on SandrPod because `E2B_DOMAIN` says so, then wrap it:

```python
from e2b import Sandbox
from langchain_e2b import E2BSandbox

# E2B_DOMAIN=your-domain  E2B_API_KEY=e2b_…
sbx = Sandbox.create()
backend = E2BSandbox(sandbox=sbx, workdir="/workspace")
```

That `workdir` is not optional. `langchain-e2b` defaults to `/home/user`, which
is E2B's image convention; SandrPod's is `/workspace`, and `/home/user` does not
exist there. More on what that used to look like below.

**Route 2 — through SandrPod's own.**

```python
from langchain_sandrpod import SandrPodClient

# SANDRPOD_API_URL=https://api.your-domain  SANDRPOD_API_TOKEN=…
client = SandrPodClient()
backend = client.create_sandbox("agent-42")
```

Either way you hand the result to the agent unchanged:

```python
from deepagents import create_deep_agent

agent = create_deep_agent(model=model, backend=backend)
```

**They coexist.** Both packages install into one virtualenv on deepagents 0.6.12
with `pip check` clean — worth saying because their declared windows differ
(`langchain-e2b` pins `>=0.6.0,<0.7.0`, `langchain-sandrpod` allows
`>=0.5.0,<0.8.0`), which reads like a conflict waiting to happen and is not one.

---

## Twelve assertions, twice

Both implement all 24 methods of `deepagents.backends.sandbox.BaseSandbox`, so
the interesting question is not "does it typecheck" but "does each call do the
thing".

| | `langchain-e2b` | `langchain-sandrpod` |
|---|---|---|
| `isinstance(backend, BaseSandbox)` | ✓ | ✓ |
| protocol methods | 24/24 | 24/24 |
| `execute` | ✓ | ✓ |
| `write` / `read` | ✓ | ✓ |
| `ls` | ✓ | ✓ |
| `edit` | ✓ | ✓ |
| `glob` | ✓ | ✓ |
| `grep` | ✓ *(see below)* | ✓ |
| `upload_files` / `download_files` | ✓ | ✓ |
| relative paths | rejected | rejected |
| execute sees what write wrote | ✓ | ✓ |
| | **12/12** | **12/12** |

Two differences in the output rather than in the outcome. `glob` returns
relative paths through E2B's backend (`probe.txt`) and absolute ones through
SandrPod's (`/workspace/probe.txt`). And `ls` shapes its entries slightly
differently. Neither breaks an agent — the model reads whatever it is handed —
but if you write code against a backend, do not assume the other one formats
identically.

### The assertion that had to be rewritten

The first version of this table was wrong, and wrong in the direction that
matters. These calls report failure in a **return value**, not by raising:

```python
ReadResult(error="File 'probe.txt': file_not_found", file_data=None)
```

My first sweep printed a green tick for that, because the call had returned
without throwing. Four checks were "passing" against a file that was never
written. A sweep that reports success for an error payload is worse than no
sweep — it manufactures confidence. Every check now fails on a populated
`error` field.

---

## The same job, three times each

Assertions prove the plumbing. An agent proves the plumbing under a model that
does not know or care which backend it has.

```
Write a Python script at /workspace/primes.py that prints the sum of all prime
numbers below 1000. Then run it and tell me the number it printed.
```

The answer is checked by re-running the script inside the container afterwards,
not by reading what the model said — an agent that reports `76127` while having
written nothing is exactly the failure worth catching.

| | `langchain-e2b` | `langchain-sandrpod` |
|---|---|---|
| correct, verified in-container | 3/3 | 3/3 |
| sandbox setup (median) | 3.0 s | 2.8 s |
| agent wall clock (median) | 6.6 s | 4.1 s |
| turns | 8, 6, 8 | 6, 6, 6 |
| tool calls | `ls` → `write_file` → `execute` (twice), `write_file` → `execute` (once) | `write_file` → `execute` (all three) |

Setup time is the same within noise — 3.0 versus 2.8 seconds is not a finding.

The agent-time gap is real across all three runs, but read it carefully: it is
**the model choosing to `ls` first** on two of the three E2B-route runs, which
costs a turn and a round trip. It is not transport speed. Why the model explored
on one path and not the other, with n=3, I cannot tell you — the tool
descriptions the two backends generate differ slightly, and that is the sort of
thing that nudges a model. I would not build a decision on this number.

---

## What testing both turned up

Three defects. None appeared in unit tests. Two of them failed *silently* —
returning something shaped like an answer.

### 1. The agent was told the file was empty

deepagents' default backend implements `grep` by shelling out:

```
grep -rHnFZ -e edited /workspace 2>/dev/null || true
```

SandrPod's sandbox image is Alpine, so `/bin/grep` was BusyBox's, which does not
support `-Z`:

```
$ grep -rHnFZ -e edited /workspace; echo exit=$?
grep: unrecognized option: Z
exit=2
```

`2>/dev/null` throws away that message. `|| true` flattens the exit code. The
backend gets an empty string back and reports **no matches**, and the agent
concludes the file does not contain what it is looking for and moves on.

There is no error anywhere in that chain. The agent gets a confident wrong
answer, which is strictly worse than a crash.

Route 2 was unaffected — `SandrPodSandbox` implements `grep` against the native
API rather than inheriting the shell-out. That is not a virtue of the design so
much as an accident of it, and it is exactly why running both paths was
worthwhile: one of them exercised code the other never touches.

Fixed by putting GNU grep in the image (`findutils` too — `find -printf` is the
same trap waiting to happen). Verified against the live deployment:

```
grep version: grep (GNU grep) 3.12
deepagents grep → [{'path': '/workspace/g.txt', 'line': 1, 'text': 'edited by the backend'}]
```

### 2. The error blamed the shell

`langchain-e2b` defaults `workdir` to `/home/user`. That directory is not in
SandrPod's image, so it is the very first thing a new integration hits:

```
Code.INTERNAL: toolbox procmgr/start: fork/exec /bin/bash: no such file or directory
```

`/bin/bash` is right there — version 5.3.9. Go's `os/exec` reports a missing
`Dir` as ENOENT against the *binary*, so the message sends you to audit your
image for a shell that was never missing.

Now:

```
toolbox procmgr/start: working directory "/home/user" is not usable: stat …: no such file or directory
```

### 3. The one that was not a bug

Relative paths do not work through `langchain-e2b`:

```python
backend.write("probe.txt", "x")   # → error: invalid_path
backend.write("/workspace/probe.txt", "x")   # → fine
```

I spent a while building a case that this was SandrPod's fault, and the evidence
looked good: the raw E2B SDK writes `"rel2.txt"` happily, and the file lands in
`/workspace` exactly as it should. Same server, same sandbox, one call works and
one does not — so the inconsistency had to be ours.

It was not. It is the first line of `langchain-e2b`'s `_write_file`:

```python
if not path.startswith("/"):
    return FileUploadResponse(path=path, error="invalid_path")
```

Its docstrings say "Absolute path" throughout. A deliberate contract, not a
defect — and I would have shipped a wrong diagnosis if I had trusted the shape
of the evidence instead of reading thirty lines of someone else's source.

---

## Which one

If you are **already running E2B code**, route 1 costs you nothing: your agent
keeps its backend, and `E2B_DOMAIN` moves the infrastructure underneath it.
Remember `workdir="/workspace"` and keep paths absolute.

If you are **starting fresh on SandrPod**, route 2 is more direct — no
compatibility layer in between, `grep` and friends implemented against the API
instead of shelled out, and the same client you use for everything else.

Both are one line to construct and neither locks you in: the backend is an
argument to `create_deep_agent`, so switching is a one-line edit, and you can
keep both installed while you decide.

Everything here — both sweeps, the agent script, the exact versions — is in the
[companion repo](https://github.com/ChangjunZhao/examples/tree/main/example/deepagents-sandbox).

- SandrPod: <https://github.com/sandrpod/sandrpod> · Apache-2.0
- Standing SandrPod up in the first place: [PRODUCTION_DEPLOYMENT.md](https://github.com/sandrpod/sandrpod/blob/main/docs/PRODUCTION_DEPLOYMENT.md)
