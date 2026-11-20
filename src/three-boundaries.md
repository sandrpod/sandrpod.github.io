# Three boundaries, and a credential that never enters the sandbox

*0.5.x isolated a filesystem. 0.6.0 asks a harder question: what if the code simply does not go through the door you built?*

*Re-verified on 2026-11-20 against v0.13.3: `hack/playground/run.sh` still passes gate, kernel, netns, egress, masking and audit.*

---

Every sandbox design has the same hole in the same place. You write a gate in
front of the file API, and the agent calls `open(2)` instead. You put a proxy in
front of the network, and the agent unsets `HTTP_PROXY`. The code you are running
was written by a model, on your behalf, and it does not have to use your door.

SandrPod 0.5.x had a good filesystem boundary: the toolbox API checked every path
against a policy, and the hardlock list kept `~/.ssh` out of reach. It was honest
about what that meant — *arbitrary code execution is not containable by the file
API* — and then left the sentence hanging. This release finishes it. Not by
making the gate cleverer, but by putting three things *underneath* it, each one
enforced where the previous one cannot see.

## Boundary one: who you may reach

The first is an egress allow-list, and the interesting part is not the list — it
is that there are three ways to hold it, and they fail differently:

| Mode | Where the rules live | When a tightened rule applies | Server unreachable |
|---|---|---|---|
| `-egress-mode=enforce` | a local file | on restart | fine |
| `-egress-mode=server` | both ends (polled copy) | within `-egress-sync` (60s) | keeps the last copy |
| `-egress-mode=decider` | the server only | next connection (≤10s cache) | **denies** |

The third one exists because a policy you cannot revoke quickly is a policy you
do not have. The cost is stated rather than spread out in footnotes: a decider
that cannot answer is a denial. An unreachable policy server must not silently
become an open proxy, and "we could not reach the rules" is not the same fact as
"the rules say no" — the proxy reports them differently on purpose.

Underneath the list sits one detail worth stealing from
[sandbox-runtime](https://github.com/anthropics/sandbox-runtime): a name is
resolved **once**, the address is vetted (loopback, link-local, cloud metadata,
this host's own interfaces), and the vetted address is what gets dialled. The
second DNS lookup is where the rebinding attack lives.

Container sandboxes get the same policy through a different mechanism, because
none of the above helps a process that ignores proxy environment variables: a
per-pod `Internal: true` network plus an `egress-proxy` sidecar, so the container
has no route except the proxy. Two deployments, one policy file, and the same
answer to "may this host leave?".

## Boundary two: the kernel, for the processes that ignore you

The second boundary is the one a model cannot walk around, because it is not an
API. With `-os-sandbox`, every process the toolbox spawns runs inside bubblewrap
(Linux) or Seatbelt (macOS) with the same shape:

- **a read-only root**, so "write the key somewhere I can read later" has nowhere
  to land;
- **the hardlocks masked** — `~/.ssh` is not off-limits to the file API, it is
  *not there*;
- **the persistence paths read-only** — `.git/hooks`, `.git/config`, `.bashrc`,
  `.mcp.json`, IDE launch configs. These are the files whose only plausible
  purpose in an agent's work tree is to run again after the sandbox is gone;
- **capabilities dropped**, and on Linux a **network namespace** whose only route
  out is the proxy — through a relay on loopback, because a namespace with no
  bridge cannot reach the proxy either.

The network part took two attempts. The first put the sandbox in its own
namespace and left it there, which is a wall with no door: `wget` could not
resolve anything, including the proxy. The relay is the door — a few dozen lines
that accept on `127.0.0.1` inside the namespace and forward to the proxy's unix
socket, bound in from the host. It carries no policy of its own, deliberately:
every byte still goes to the proxy, which is where the decision lives.

The capability drop has a matching subtler decision. `--cap-drop ALL` is right
until the agent runs as root, which is how a system service runs, and then it
breaks the ordinary case of serving a user's home directory: root without
`CAP_DAC_OVERRIDE` cannot read files owned by someone else. The release keeps
exactly two capabilities for that case — `CAP_DAC_OVERRIDE` and
`CAP_DAC_READ_SEARCH` — and keeps `CAP_SYS_ADMIN` gone, because that is the one
that would `umount` every mask above it.

## Boundary three: the credential the agent never holds

The third is the one that made me rewrite a paragraph of documentation twice.

An agent that can call a paid API needs a key. A key in its environment is a key
it can print, send to a host the policy allows, and paste into a repository. So
`-credential-mask` replaces the real value with `fake_value_<uuid4>` inside the
sandbox, and substitutes it back at the proxy — over TLS the proxy terminates.

That sentence is where the honesty has to start, because TLS termination is not a
neutral act. For a terminated host, the certificate the sandbox sees is signed by
*us*; pinning breaks, and mTLS to the upstream cannot work. What buys that is the
only general answer to "use this key but do not let the model copy it": the real
bytes exist in the proxy's memory and in the request it sends, nowhere the sandbox
can reach. Hosts that need a real end-to-end channel go in
`-credential-terminate-exclude` and get no masking — a trade, not an oversight —
and a masked credential over plain HTTP is refused rather than leaked.

Two implementation notes, because both are where this kind of feature usually
rots. Substitution in a body is length-preserving, so `Content-Length` stays
valid and the proxy never has to re-frame. And an AWS SigV4 request is *re-signed*
with the real secret, because the signature is an HMAC over the whole request: a
sentinel in the `Credential` scope is not a value you can swap in place. The
re-signer is checked against AWS's published example signature, because a
re-signer that is subtly wrong fails exactly like a wrong secret, and that is a
bug that hides for months.

## The refusals have two readers

One small change that turned out to matter more than expected: every refusal now
carries two explanations.

```
Reason:      路径 "/Users/me/.ssh/id_rsa" 被 hardlock 锁定（可用 sandrpod-tray unlock 解锁）
ModelReason: /Users/me/.ssh/id_rsa is hard-locked by the operator's policy and cannot be
             read or written; do not retry, and ask the user to unlock it in the tray if
             this is genuinely needed
```

The first is for the person in the dialog. The second is for the model, and it is
what the tool result returns — one line of English with what was refused and what
to do instead. Both go into the audit record. The split is not cosmetic: a
localized UI string handed to a model is noise it cannot act on, and an English
imperative in a consent dialog reads as a bug to whoever has to click it.

## The fleet has to agree with you

A walled sandbox only means something if the machine that runs it can enforce the
wall. A worker from before this release ignores the policy field entirely: it
starts the container, reports `RUNNING`, and the API serves the policy back as if
it were in force.

So the worker now advertises what it can enforce on the tunnel handshake, and the
control plane refuses to place a policy on a worker that does not advertise it:

```
409 worker capability unavailable: this sandbox asks for an egress policy, but
1 online local worker(s) do not support it (upgrade the poder to a build that
advertises "egress", or drop the policy)
```

The agent gets the mirror image. `-egress-mode=server|decider` probe
`GET /api/v1/capabilities` and refuse to start against a control plane that
predates them, instead of running deny-all and looking like a network outage. A
404 on a route you invented for the purpose is unambiguous; a 404 on a sandbox
record is not, and that difference is the whole design.

## What testing this actually cost

The parts above are the design. The bugs are the interesting part, and they share
a property: every one of them was invisible to the layer that "owned" it.

- **The OS sandbox never ran through the product.** `Resolve` returned `bwrap`
  as a bare name and the toolbox assigned it to `exec.Cmd.Path`; `execve` does no
  `PATH` lookup, so every `Execute` died with
  `fork/exec bwrap: no such file or directory`. The package's own tests passed
  because they call `Command()` and hand the result to `exec.Command`, which
  looks the name up itself. Found by a manual walkthrough, on its first command.
- **The PTY path never wrapped at all.** `ProcManager.Start` returned from its
  PTY branch before the sandbox call, so an interactive session ran a shell
  completely outside bubblewrap — the one path the layer exists for.
- **Masked SigV4 requests were never re-signed.** Header substitution rewrote the
  credential scope to the real access key before the re-signer looked for the
  sentinel, so the lookup missed and the request went out signed with a key id it
  did not match. Every masked AWS call would have failed with
  `SignatureDoesNotMatch`.
- **A masked credentials file inside a hardlocked directory was invisible.** The
  substitution was mounted before the hardlock masks, and the default seeds
  hardlock `~/.aws` wholesale, so the documented example produced a sandbox with
  no credentials file at all.
- **An inherited `NO_PROXY` disabled the proxy.** The child environment is the
  agent's environment plus ours, so a `NO_PROXY` from the launching shell survived,
  and every proxy-aware client went direct.
- **The test harness skipped itself on CI and still went green.** The nightly's
  legacy phase is launched with `if ! server & then`, and a background launch
  always succeeds — so the negation was always true and the phase printed
  "skipping" on every run. It passed locally because I had run that branch by
  hand. That one is about the harness, and it is the most instructive of the set:
  a skip with a reason is still a skip, and only the real path proves anything.

None of these were found by unit tests, and all of them now have one — but the
tests came *after* the failure, which is the only order that ever works for this
class of bug. What found them was running the assembled product: a privileged
container with bubblewrap, a real agent, a real control plane, a CLI, and an
upstream TLS server that reports what it received. That harness
(`hack/playground/run.sh`) now runs nightly, and its first few runs found four
bugs, then two more.

## Upgrading, and the one step that matters

Workers first, then the control plane, then agents. Only the first step is
mandatory: a 0.5 worker ignores the policy field, and this control plane answers
`409` rather than creating a sandbox that looks walled. A sandbox *without* a
policy still schedules onto an old worker, so a staged rollout is fine.

The migration is additive (`ADD COLUMN … DEFAULT ''` on two tables), so an old
binary still reads a database a new one migrated. Everything in the release is
off by default: a deployment that upgrades without touching flags behaves exactly
as it did, including on the agent path.

Three Go-level breaks for anyone importing `pkg/…`: `permission.AuditSink.Record`
gained a parameter, `PoderRepository` gained `SelectBestFor`, and
`egress.Server.SOCKSURL()` returns `""` in unix-socket mode. The HTTP API, the
wire format and the CLI are additive.

## What is still not done

- **macOS Seatbelt is unverified.** The profile cannot be applied from a shell
  that is itself already sandboxed, which is what an MDM-managed developer machine
  turns out to be, so it ships behind its probe and is documented as experimental.
  Linux is verified against real sandboxed processes.
- **The write-protected paths are mounts, not a security model.** A hardlink to a
  protected file writes the same inode, and a command that creates `.git/hooks`
  and writes into it in the same breath wins — the protection applies from the
  next spawn. Closing those needs Landlock.
- **Terminated tunnels speak HTTP/1.1 only**, and masking does not cover what an
  upstream echoes back.
- **The audit trail is a log, not an alerting system.** It records which
  credential went where and which connection had its TLS terminated; nobody is
  paged when that stops.

The release is
[v0.6.0](https://github.com/sandrpod/sandrpod/releases/tag/v0.6.0): binaries for
Linux, macOS and Windows, images for amd64 and arm64, and
[the notes](https://github.com/sandrpod/sandrpod/blob/main/docs/RELEASE_NOTES.md)
with the same upgrade order and the same limits. If you try it and something
reads wrong, the failure modes are described in
[OS_SANDBOX.md](https://github.com/sandrpod/sandrpod/blob/main/docs/OS_SANDBOX.md),
[EGRESS_POLICY.md](https://github.com/sandrpod/sandrpod/blob/main/docs/EGRESS_POLICY.md)
and
[CREDENTIALS.md](https://github.com/sandrpod/sandrpod/blob/main/docs/CREDENTIALS.md)
— including the parts that do not work yet.