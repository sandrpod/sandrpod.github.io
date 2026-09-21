# Self-hosting an E2B-compatible sandbox stack, from an empty server

*One VM, one domain, four containers. At the end, the unmodified E2B SDK is
talking to infrastructure you own — and we check it feature by feature.*

---

AI agents write code, and that code has to run somewhere. E2B gives you a clean
answer: a sandbox as an API call. It works, the SDK is good, and for a lot of
teams that is the end of the story.

For the rest of us it is the beginning of a different one. The code an agent
writes is often *your customers'* code, or it touches your data, and "it runs in
someone else's cloud" turns into a procurement question you cannot win. Or the
bill grows superlinearly with your agent traffic. Or you are in a jurisdiction
where the data simply cannot leave.

The good news is that "self-hosted" and "throw away your SDK" are not the same
requirement. This walkthrough stands up [SandrPod](https://github.com/sandrpod/sandrpod)
— open-source, Apache-2.0 — on a bare server, terminates a wildcard TLS
certificate in front of it, and then points the **official, unmodified** `e2b`
Python SDK at it by setting two environment variables. Then we exercise the
client surface function by function and report what works, what differs, and
what does not exist yet.

Everything below was run start to finish on a fresh CentOS Stream 10 VM. The
outputs are real, including the failures.

---

## 1. The shape of the thing

E2B's architecture is: a control plane you talk to over REST, and a daemon
called `envd` inside every sandbox that the control plane proxies your
filesystem, process, and PTY calls through to.

SandrPod's is: a control plane you talk to over REST, and a daemon called
`toolbox` inside every sandbox that the control plane reaches **through a
reverse tunnel** the worker dials out on.

```
        your laptop
             │  HTTPS
             ▼
    ┌────────────────────┐
    │   control plane    │   api.<domain>         → REST
    │      (server)      │   <port>-<id>.<domain> → sandbox
    └─────────┬──────────┘
              │  WebSocket + yamux, dialled OUTBOUND by the worker
    ┌─────────▼──────────┐
    │   worker (poder)   │
    └─────────┬──────────┘
              │  Docker API
    ┌─────────▼──────────┐
    │  sandbox container │  ← toolbox (≈ envd) lives here
    └────────────────────┘
```

That shape matters more than it looks. Because the topologies match —
control-plane-proxies-to-in-sandbox-daemon, with `toolbox` sitting exactly where
`envd` sits — supporting E2B's SDK is **protocol adaptation, not a second
architecture**. Nothing in the diagram exists to serve compatibility; the
compatibility falls out of a design that was already there.

The tunnel direction is the other reason to care: the worker dials the control
plane, so a machine running sandboxes needs **no inbound ports at all**. You can
put one behind a NAT, on an office network, or on a laptop, and it still joins
the pool.

---

## 2. From an empty server

One VM: 8 vCPU, 15 GB RAM, CentOS Stream 10, nothing installed. One domain with
its nameservers at a provider whose DNS has an API — here Tencent Cloud's
DNSPod, but any lego-supported provider works the same way.

### 2.1 Docker

```bash
dnf -y install dnf-plugins-core
curl -fsSL https://download.docker.com/linux/centos/docker-ce.repo \
  -o /etc/yum.repos.d/docker-ce.repo
dnf -y install docker-ce docker-ce-cli containerd.io \
  docker-buildx-plugin docker-compose-plugin
systemctl enable --now docker
```

On this particular image that failed twice, and both failures are worth knowing
because they are properties of minimal cloud kernels, not of anything here:

```
failed to register "bridge" driver: ... iptables --wait -t nat -A PREROUTING
-m addrtype --dst-type LOCAL -j DOCKER: Warning: Extension addrtype revision 0
not supported, missing kernel module?
```

`xt_addrtype` lives in `kernel-modules-extra`, which was not installed — and
`dnf` could not find a build matching the running kernel, so it had silently
satisfied the dependency with the *debug* variant of a *different* kernel
version. Rather than chase kernel packages, switch Docker to its nftables
firewall backend, which does not need the module:

```bash
cat > /etc/docker/daemon.json <<'JSON'
{ "firewall-backend": "nftables" }
JSON
```

Then:

```
failed to start daemon: ... IPv4 forwarding is disabled
```

```bash
echo 'net.ipv4.ip_forward = 1' > /etc/sysctl.d/99-docker.conf
sysctl -p /etc/sysctl.d/99-docker.conf
systemctl restart docker
```

```
$ docker version --format 'server={{.Server.Version}} api={{.Server.APIVersion}}'
server=29.6.2 api=1.55
```

### 2.2 DNS: two records, one of them a wildcard

```
api.example.com   A   203.0.113.10
*.example.com     A   203.0.113.10
```

The wildcard is not optional and it is the single thing that separates this from
a quickstart. E2B addresses a service inside a sandbox as
`<port>-<sandboxID>.<domain>` — and the sandbox ID does not exist until the
sandbox does. You cannot enumerate those names in advance, so you cannot create
records for them in advance.

Any way of creating the two records is fine; the console works. Doing it over
the API keeps the whole deployment scriptable. A ~90-line stdlib-only Python
client for DNSPod's TC3-signed API is in the [companion repo](https://github.com/ChangjunZhao/examples/tree/main/example/e2b-compatible) (`tcdns.py`):

```bash
export TENCENTCLOUD_SECRET_ID=... TENCENTCLOUD_SECRET_KEY=...
./tcdns.py upsert example.com api '203.0.113.10'
./tcdns.py upsert example.com '*' '203.0.113.10'
```

Check from the server, and check a name that does not exist yet — that is the
one that matters:

```
$ getent ahostsv4 8000-abc.example.com | head -1
203.0.113.10
```

### 2.3 A wildcard certificate, via DNS-01

Let's Encrypt will only issue wildcards through the DNS-01 challenge, so the
ACME client needs credentials for the DNS provider. [lego](https://go-acme.github.io/lego/)
has plugins for ~150 of them.

```bash
export TENCENTCLOUD_SECRET_ID=... TENCENTCLOUD_SECRET_KEY=...
lego --email you@example.com --accept-tos \
     --dns tencentcloud --dns.propagation-wait 90s \
     -d example.com -d '*.example.com' \
     --path /etc/lego run
```

> These are lego 4.x flags. lego 5 moved every flag under `run`, renamed
> `--dns.propagation-wait` to `--dns.propagation.wait`, and dropped the `renew`
> subcommand — if `lego --version` says 5, use the form in the
> [deployment guide](https://github.com/sandrpod/sandrpod/blob/main/docs/PRODUCTION_DEPLOYMENT.md#3-a-wildcard-certificate).

The `--dns.propagation-wait` is worth setting generously. lego polls the
authoritative nameservers for the TXT record it just wrote, and DNSPod took
around 100 seconds per domain to serve it consistently:

```
[INFO] [*.example.com] acme: Checking DNS record propagation. [nameservers=183.60.83.19:53,183.60.82.98:53]
[INFO] Wait for propagation [timeout: 1m0s, interval: 2s]
[INFO] [*.example.com] The server validated our request
```

```
$ openssl x509 -in /etc/lego/certificates/example.com.crt -noout -ext subjectAltName
    DNS:*.example.com, DNS:example.com
```

Note the filename: lego names the bundle after the **first** `-d`, not after the
wildcard.

Renewal is a daily systemd timer that no-ops until there are fewer than 30 days
left, and signals the proxy to reload when it does renew:

```ini
# /etc/systemd/system/lego-renew.service
[Service]
Type=oneshot
EnvironmentFile=/root/sandrpod-deploy/tencent.env
ExecStart=/usr/local/bin/lego --email you@example.com --accept-tos \
  --dns tencentcloud --dns.propagation-wait 90s \
  -d example.com -d *.example.com \
  --path /etc/lego renew --days 30
ExecStartPost=-/usr/bin/docker kill --signal=SIGUSR1 sandrpod-caddy
```

### 2.4 Four containers

```yaml
services:
  postgres:
    image: postgres:16-alpine
    environment:
      POSTGRES_USER: sandrpod
      POSTGRES_PASSWORD: ${POSTGRES_PASSWORD:?}
      POSTGRES_DB: sandrpod
    volumes: [pgdata:/var/lib/postgresql/data]
    networks: [sandrpod]
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U sandrpod -d sandrpod"]
      interval: 5s

  server:
    image: ghcr.io/sandrpod/server:v0.5.5
    command:
      - "-port=8080"
      - "-db=postgres://sandrpod:${POSTGRES_PASSWORD}@postgres:5432/sandrpod?sslmode=disable"
    environment:
      SANDRPOD_TOKEN: ${SANDRPOD_TOKEN:?}
      SANDRPOD_E2B_DOMAIN: example.com     # ← turns on the E2B host router
    ports:
      - "127.0.0.1:8080:8080"               # admin API, loopback only
    networks: [sandrpod]

  poder:
    image: ghcr.io/sandrpod/poder:v0.5.5
    volumes: [/var/run/docker.sock:/var/run/docker.sock]
    environment:
      API_URL: http://server:8080
      SANDRPOD_TOKEN: ${SANDRPOD_TOKEN:?}
      REGION: local
      PROVIDER_TYPE: local
      SANDRPOD_NETWORK: sandrpod
    networks: [sandrpod]

  caddy:
    image: caddy:2-alpine
    ports: ["80:80", "443:443"]
    volumes:
      - ./Caddyfile:/etc/caddy/Caddyfile:ro
      - /etc/lego/certificates:/certs:ro
    networks: [sandrpod]

networks:
  sandrpod:
    name: sandrpod          # ← pin it; see below
```

Four details in there are load-bearing.

**`SANDRPOD_TOKEN: ${SANDRPOD_TOKEN:?}`.** The `:?` makes Compose refuse to
start when the variable is empty. An empty token means every request runs as an
anonymous admin, which is survivable on a laptop and catastrophic on a public
IP. Failing to boot is better than booting wrong.

**`SANDRPOD_NETWORK: sandrpod`, and `name: sandrpod` on the network.** The worker
passes that string straight to the Docker API when it creates sandbox
containers, so it must be the network's *real* Docker name — and Compose prefixes
network names with the project name unless you pin them. Get this wrong and the
failure is nasty: sandboxes are created successfully, land on a different
network, and every exec hangs until it times out.

**`REGION: local`.** The E2B gateway schedules onto `provider=local` /
`region=local` for the local substrate. A worker registered as, say,
`cn-guangzhou` is filtered out by the scheduler, and `Sandbox.create()` returns
`500: no available local poder found` while `/api/v1/poders` cheerfully shows it
ONLINE. (Set `SANDRPOD_E2B_PROVIDER` instead if you want `Sandbox.create()` to
provision real cloud VMs.)

**`127.0.0.1:8080:8080`.** Once `SANDRPOD_E2B_DOMAIN` is set, *every* host under
that domain belongs to the E2B gateway — the native admin API returns 404 on
`api.example.com`. So bind it to loopback and reach it by SSH, which has the
pleasant side effect of keeping the admin surface off the internet entirely.

The Caddy config is short, and one of its defaults is doing real work:

```caddyfile
{
	auto_https off      # lego owns the certificate; DNS-01 is the only way to get a wildcard
	admin off
}

:443 {
	tls /certs/example.com.crt /certs/example.com.key

	reverse_proxy sandrpod-server:8080 {
		flush_interval -1     # streamed command output must not be buffered
	}
}

:80 {
	redir https://{host}{uri} permanent
}
```

A single `:443` block serves every hostname, because a wildcard certificate is
valid for all of them and the backend routes on `Host` itself. **Caddy forwards
the original `Host` header by default** — which is the whole routing mechanism
here. On nginx you must write `proxy_set_header Host $host;` explicitly, or
every sandbox URL silently stops resolving to a sandbox.

```
$ docker compose up -d --wait
 Container sandrpod-postgres  Healthy
 Container sandrpod-server    Healthy
 Container sandrpod-poder     Healthy
 Container sandrpod-caddy     Healthy
```

### 2.5 An API key shaped like E2B's

The E2B SDK validates the key client-side against `/^e2b_[0-9a-f]+$/`, so keys
are issued in that shape and are drop-in as `E2B_API_KEY`. Only the hash is
stored; the bare key is shown once.

```bash
curl -s -X POST -H "Authorization: Bearer $SANDRPOD_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"name":"demo","role":"user"}' \
  http://127.0.0.1:8080/api/v1/tokens
```

```json
{"key":"e2b_2c287f65...","prefix":"e2b_2c287f653086","name":"demo","role":"user"}
```

---

## 3. Two environment variables

That is the whole client-side change. No fork, no patched SDK, no shim:

```bash
pip install e2b e2b-code-interpreter     # official packages, untouched
export E2B_DOMAIN=example.com
export E2B_API_KEY=e2b_2c287f65...
```

```python
from e2b import Sandbox

sbx = Sandbox.create()
print(sbx.sandbox_id, sbx.is_running())
print(sbx.commands.run("echo hello from $(hostname); uname -m").stdout)
sbx.files.write("/tmp/hi.txt", "written through the E2B SDK\n")
print(sbx.files.read("/tmp/hi.txt"))
sbx.kill()
```

```
e2bb62bc53334e83f0f True
hello from 6c22852f8f0b
x86_64
written through the E2B SDK
```

Measured from a laptop over the public internet — so these numbers include real
round-trip time, not just server work:

| | median |
|---|---|
| `Sandbox.create()` → usable | **2.7 s** |
| `commands.run` round trip | **217 ms** |
| `run_code` round trip | **194 ms** |

(The first call after a cold image pull is much slower — budget ~20 s once.)

---

## 4. The client surface, function by function

`e2b` **2.35.0** and `e2b-code-interpreter` **2.9.0**, against the deployment
above. Every row below really executed.

### Lifecycle — 11/11

| Call | Result |
|---|---|
| `Sandbox.create()` | `e2b6c1b0f2b8a72dd40` in 2.9 s |
| `is_running()` | `True` |
| `get_info()` | `state=running`, `envd_version=0.2.0`, cpu/mem populated |
| `set_timeout(600)` | ok — maps to the sandbox's idle TTL |
| `Sandbox.list()` | found |
| `Sandbox.list(query=SandboxQuery(metadata={...}))` | found — metadata round-trips and filters |
| `Sandbox.connect(id)` | reattached |
| `pause()` / resume via `connect()` | `True`, then `echo back` → `back` |
| `kill()`, then `is_running()` | `True`, then `False` |

### Filesystem — 11/11

`write` · `read` · `read(format="bytes")` · `exists` · `get_info` · `make_dir` ·
`list` · `rename` · `remove` · `write_files` (batch multipart) · `watch_dir`

```
watch_dir  →  2 events: [('watched.txt', CREATE), ('watched.txt', WRITE)]
```

### Commands — 7/7

`run` foreground · non-zero exit raising `CommandExitException(exit_code=3)` ·
`run(background=True)` returning a real pid · `list` · `send_stdin` · `kill` ·
and incremental streaming:

```
connect (streaming)  →  3 separate chunks, exit=0
```

Three chunks, not one buffered blob — which is what `flush_interval -1` in the
Caddy config is protecting.

### PTY — 1/1

`create` / `send_stdin` / `resize` / `kill`, a real terminal session:

```
killed=True, echoed 'pty-works'=True, 16 frames
```

### Code interpreter — 14/14 (run_code 5, stateful 2, contexts 5, charts 2)

Stateful kernel, which is the whole point of this SDK:

```python
sbx.run_code("a = 100")
sbx.run_code("a * 2").text        # → '200'
```

Contexts are independent namespaces, and `restart` clears one without
destroying it:

```
create                 → 1bba3fa6bdceaa3e…, z=7 written
list                   → 1 context
isolation              → z=7 inside the context, NameError in the default one
restart                → NameError: name 'z' is not defined
remove                 → removed
```

Errors come back structured rather than as text:

```
run_code("1/0")  →  ZeroDivisionError: division by zero
```

And charts are captured, not just printed. A `matplotlib` figure drawn inside
the sandbox arrives as a PNG in `Execution.results[].png`, plus structured chart
metadata:

```
matplotlib → PNG          19276 bytes, header b'\x89PNG'
chart metadata            BarChart
```

![A matplotlib line chart generated inside the self-hosted sandbox](./chart-from-sandbox.png)

### Ports and previews — 1/1

Start something in the sandbox, ask for its host, open it:

```python
sbx.commands.run("cd /workspace && python3 -m http.server 8000 &", background=True)
host = sbx.get_host(8000)     # 8000-e2ba4581bfc832c3459.example.com
```

```
$ curl https://8000-e2ba4581bfc832c3459.example.com/index.html
<h1>served from the sandbox</h1>
```

No credential, because a browser cannot send one — possession of the
unguessable hostname is the capability, as on E2B. The envd RPC surface on the
same wildcard domain is *not* included in that:

```
GET https://49983-<id>.example.com/files          → 401
GET https://49983-<id>.example.com/files + key    → 200
```

If every consumer in your deployment is a program that can carry a key, set
`SANDRPOD_E2B_PRIVATE_PORTS=1` and the preview URLs require one too.

### Metrics — 1/1

```
get_metrics()  →  1 sample, cpu=0%, mem=1044590592
```

---

## 5. What this shook out

Standing this up found three real defects, all of which had gone unnoticed
because the previous compatibility pass ran against the plain-HTTP debug
listener — where requests never traverse the `<port>-<sandboxID>.<domain>` host
router, so none of them can occur.

- **`is_running()` always returned `False`.** The SDK probes `GET /health` on
  the envd host and reads 502 as "not running". `/health` was not a routed envd
  path, so it fell through to the generic port proxy, which dialled
  `127.0.0.1:49983` inside the container, found nothing listening, and returned
  502 — for a perfectly healthy sandbox.
- **Code-interpreter contexts were entirely unusable** (401). The SDK sends
  `X-Access-Token` on `/execute` but *no credential at all* on `/contexts` —
  an asymmetry that only shows up once auth is enforced on that host.
- **Preview URLs required an API key**, which no browser can supply.

All three are fixed as of **v0.5.2**, each with a regression test that was
confirmed to fail with its fix reverted. That is the honest argument for doing
this exercise on a real domain rather than a laptop: the debug listener will
tell you the protocol is implemented; only the wildcard host router tells you it
is *correct*.

### Not implemented

Reported plainly, because finding out at integration time is worse:

| Call | Status |
|---|---|
| `create_snapshot()` | `405` — not implemented |
| `fork()` | `405` — not implemented |
| `envVars` on create | accepted, but not exercised by the real SDK yet |

`get_mcp_url()` and `download_url()` do return well-formed URLs
(`50005-<id>.<domain>/mcp` and `49983-<id>.<domain>/files?path=…`); this sweep
only checked that they are produced, not that every consumer of them works.

**Total: 50 checks, 48 passing.** The two failures are the 405s above.

---

## 6. What it costs to keep

One VM. The stack idles at four containers; sandboxes are Docker containers
created on demand and reclaimed on an idle TTL you set per sandbox
(`set_timeout`). Certificate renewal is a systemd timer. Postgres holds
sandboxes, jobs, workers, and API-token hashes — back that up and you can
rebuild everything else from the Compose file.

Scaling out is adding workers, not resizing this one: each `poder` dials the
control plane over its outbound tunnel, so a second machine — another cloud,
another region, an office box behind NAT — joins by running one container with
no inbound firewall changes at all.

---

## Reproduce it

Everything here — the Compose file, the Caddyfile, `tcdns.py`, the lego units,
and both test sweeps — is in the [companion repo](https://github.com/ChangjunZhao/examples/tree/main/example/e2b-compatible). The whole path from an
empty server to a passing sweep is four scripts.

- SandrPod: <https://github.com/sandrpod/sandrpod> (Apache-2.0)
- `pip install sandrpod-cli`

If you try it against a different DNS provider or a different distro, the two
places to look first are the wildcard record and the `Host` header — in that
order. Everything else in this walkthrough was easy.
