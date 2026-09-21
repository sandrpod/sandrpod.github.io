# 从一台空服务器开始，自建一套兼容 E2B 的沙箱服务

*一台机器，一个域名，四个容器。跑完之后，未经修改的官方 E2B SDK
连的是你自己的基础设施——并且我们逐个功能验过。*

---

AI Agent 会写代码，那些代码总得找个地方跑。

跑在自己的服务进程里是不行的——Agent 生成的代码你没法预先审查，一个
`rm -rf` 或者一个死循环就能带走整台机器。所以业界的答案是沙箱：一次 API
调用换一个隔离的执行环境，跑完就扔。

海外这块做得最好的是 [E2B](https://e2b.dev)，SDK 设计得很干净，托管服务开箱即用。
对很多团队来说故事到这儿就结束了。

对国内团队来说故事才刚开始。三个绕不过去的问题：

**数据出不去。** Agent 处理的往往是客户的代码、客户的数据。"跑在别人家的云上"
这句话在合规评审里基本没有赢面，尤其是金融、政务、医疗这些行业。

**云厂商不对。** 你的基础设施在阿里云或者腾讯云上，海外沙箱服务不覆盖国内云——
意味着每一次执行都要跨境往返，延迟和稳定性都是问题。

**账单随 Agent 流量超线性增长。** Agent 的调用量本来就难预测，按次计费的东西
一旦跑起来，成本曲线不好看。

好消息是"自托管"和"扔掉 SDK 重写一遍"不是同一件事。

这篇文章从一台什么都没装的服务器开始，部署
[SandrPod](https://github.com/sandrpod/sandrpod)（开源，Apache-2.0），在前面
架一张通配符 TLS 证书，然后**只改两个环境变量**，让官方的、未经任何修改的
`e2b` Python SDK 连上来。最后逐个函数地跑一遍客户端接口，如实报告哪些能用、
哪些有差异、哪些还没有。

底下所有内容都是在一台全新的 CentOS Stream 10 机器上从头跑完的，输出是真的，
包括失败的部分。

---

## 一、先看形状

E2B 的架构是：一个你通过 REST 访问的控制平面，加上每个沙箱里一个叫 `envd`
的守护进程，控制平面把你的文件、进程、PTY 调用代理进去。

SandrPod 的架构是：一个你通过 REST 访问的控制平面，加上每个沙箱里一个叫
`toolbox` 的守护进程，控制平面**通过一条 worker 主动拨出的反向隧道**够到它。

```
        你的笔记本
             │  HTTPS
             ▼
    ┌────────────────────┐
    │      控制平面      │   api.<domain>         → REST
    │     (server)       │   <port>-<id>.<domain> → 沙箱
    └─────────┬──────────┘
              │  WebSocket + yamux，由 worker 主动向外拨出
    ┌─────────▼──────────┐
    │   worker (poder)   │
    └─────────┬──────────┘
              │  Docker API
    ┌─────────▼──────────┐
    │     沙箱容器       │  ← toolbox（≈ envd）住在这里
    └────────────────────┘
```

这个形状比看上去重要。因为两边的拓扑是同构的——都是控制平面代理到沙箱内守护
进程，`toolbox` 待的位置正是 `envd` 待的位置——所以支持 E2B 的 SDK 是**协议
适配，不是第二套架构**。图里没有任何一个组件是为了兼容而存在的；兼容是从一个
本来就长这样的设计里掉出来的。

隧道的方向是另一个值得在意的点：worker 主动连控制平面，所以一台跑沙箱的机器
**完全不需要任何入站端口**。你可以把它放在 NAT 后面、放在办公室网络里、放在
一台笔记本上，它照样加入集群。这对国内的网络环境挺实用——很多机器根本申请不到
公网入口。

---

## 二、从一台空服务器开始

一台机器：8 核 15G，CentOS Stream 10，什么都没装。一个域名，NS 指向一个
带 API 的 DNS 服务商——这里用腾讯云 DNSPod，但 lego 支持的一百多家都一样。

### 2.1 装 Docker

```bash
dnf -y install dnf-plugins-core
curl -fsSL https://download.docker.com/linux/centos/docker-ce.repo \
  -o /etc/yum.repos.d/docker-ce.repo
dnf -y install docker-ce docker-ce-cli containerd.io \
  docker-buildx-plugin docker-compose-plugin
systemctl enable --now docker
```

在这个镜像上它失败了两次，两次都值得知道，因为它们是精简版云内核的通病，
跟这套东西本身无关：

```
failed to register "bridge" driver: ... iptables --wait -t nat -A PREROUTING
-m addrtype --dst-type LOCAL -j DOCKER: Warning: Extension addrtype revision 0
not supported, missing kernel module?
```

`xt_addrtype` 在 `kernel-modules-extra` 包里，没装——而 `dnf` 找不到与当前
运行内核匹配的构建，于是它悄悄用**另一个内核版本的 debug 变体**满足了依赖。
与其去追内核包，不如让 Docker 换用 nftables 防火墙后端，那条路不需要这个模块：

```bash
cat > /etc/docker/daemon.json <<'JSON'
{ "firewall-backend": "nftables" }
JSON
```

然后：

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

### 2.2 DNS：两条记录，其中一条是通配符

```
api.example.com   A   203.0.113.10
*.example.com     A   203.0.113.10
```

通配符不是可选项，而且它是这篇文章和一个 quickstart 之间唯一的区别。E2B
访问沙箱内服务的地址形如 `<port>-<sandboxID>.<domain>`——而沙箱 ID 在沙箱
被创建出来之前根本不存在。你没法提前枚举这些名字，也就没法提前建记录。

用什么方式建这两条都行，控制台就够。走 API 的好处是整个部署可脚本化。
[配套仓库](https://github.com/ChangjunZhao/examples/tree/main/example/e2b-compatible)
里有一个约 90 行、只用标准库的 DNSPod TC3 签名客户端（`tcdns.py`）：

```bash
export TENCENTCLOUD_SECRET_ID=... TENCENTCLOUD_SECRET_KEY=...
./tcdns.py upsert example.com api '203.0.113.10'
./tcdns.py upsert example.com '*' '203.0.113.10'
```

在服务器上验证，而且要去解一个**还不存在的**名字——那个才是关键：

```
$ getent ahostsv4 8000-abc.example.com | head -1
203.0.113.10
```

### 2.3 通配符证书，走 DNS-01

Let's Encrypt 只通过 DNS-01 挑战签发通配符证书，所以 ACME 客户端需要 DNS
服务商的凭据。[lego](https://go-acme.github.io/lego/) 有约 150 家的插件，
国内几家主流云都在列。

```bash
export TENCENTCLOUD_SECRET_ID=... TENCENTCLOUD_SECRET_KEY=...
lego --email you@example.com --accept-tos \
     --dns tencentcloud --dns.propagation-wait 90s \
     -d example.com -d '*.example.com' \
     --path /etc/lego run
```

> 以上是 lego 4.x 的写法。lego 5 把所有参数挪到了 `run` 子命令下，把
> `--dns.propagation-wait` 改名为 `--dns.propagation.wait`，还去掉了 `renew`
> 子命令——如果 `lego --version` 显示的是 5.x，请照
> [部署指南](https://github.com/sandrpod/sandrpod/blob/main/docs/PRODUCTION_DEPLOYMENT.md#3-a-wildcard-certificate)里的写法来。

`--dns.propagation-wait` 值得给得宽松些。lego 会去轮询权威 NS 查它刚写进去
的 TXT 记录，实测 DNSPod 每个域名要一百秒左右才能稳定返回：

```
[INFO] [*.example.com] acme: Checking DNS record propagation. [nameservers=183.60.83.19:53,183.60.82.98:53]
[INFO] Wait for propagation [timeout: 1m0s, interval: 2s]
[INFO] [*.example.com] The server validated our request
```

```
$ openssl x509 -in /etc/lego/certificates/example.com.crt -noout -ext subjectAltName
    DNS:*.example.com, DNS:example.com
```

注意文件名：lego 用**第一个** `-d` 给证书包命名，不是用通配符那个。

续期是一个每天跑的 systemd timer，剩余不足 30 天才真的动作，续成功后给反代
发信号重载：

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

### 2.4 四个容器

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
      SANDRPOD_E2B_DOMAIN: example.com     # ← 打开 E2B 的 Host 路由
    ports:
      - "127.0.0.1:8080:8080"               # 管理 API，只绑回环
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
    name: sandrpod          # ← 必须钉死，见下
```

里面有四处是承重的。

**`SANDRPOD_TOKEN: ${SANDRPOD_TOKEN:?}`。** 那个 `:?` 让 Compose 在变量为空时
拒绝启动。空 token 意味着每个请求都以匿名管理员身份执行——在笔记本上还能活，
在公网 IP 上是灾难。起不来比起错了强。

**`SANDRPOD_NETWORK: sandrpod`，以及网络上的 `name: sandrpod`。** worker 创建
沙箱容器时把这个字符串原样传给 Docker API，所以它必须是网络的**真实** Docker
名字——而 Compose 默认会给网络名加项目名前缀，除非你钉死。这个搞错了故障很恶心：
沙箱创建成功、落在另一个网络上、每一次执行都挂到超时为止。

**`REGION: local`。** E2B 网关调度到 `provider=local` / `region=local` 这个本地
底座上。一个注册成比如 `cn-guangzhou` 的 worker 会被调度器过滤掉，于是
`Sandbox.create()` 返回 `500: no available local poder found`，而
`/api/v1/poders` 还乐呵呵地显示它 ONLINE。（如果你想让 `Sandbox.create()`
去真的开云主机，改设 `SANDRPOD_E2B_PROVIDER`。）

**`127.0.0.1:8080:8080`。** 一旦设了 `SANDRPOD_E2B_DOMAIN`，该域名下的**每个**
主机名都归 E2B 网关管——原生管理 API 在 `api.example.com` 上会返回 404。所以
把它绑到回环、通过 SSH 访问，顺带的好处是管理面完全不在公网上。

Caddy 的配置很短，而它的一个默认行为在干实事：

```caddyfile
{
	auto_https off      # 证书归 lego 管；通配符只能走 DNS-01
	admin off
}

:443 {
	tls /certs/example.com.crt /certs/example.com.key

	reverse_proxy sandrpod-server:8080 {
		flush_interval -1     # 流式命令输出不能被缓冲
	}
}

:80 {
	redir https://{host}{uri} permanent
}
```

一个 `:443` 块伺候所有主机名，因为通配符证书对它们全都有效，而后端本身就是按
`Host` 路由的。**Caddy 默认转发原始 `Host` 头**——这正是整套路由机制的基础。
换成 nginx 你必须显式写 `proxy_set_header Host $host;`，否则每个沙箱 URL 都会
悄无声息地不再指向沙箱。

```
$ docker compose up -d --wait
 Container sandrpod-postgres  Healthy
 Container sandrpod-server    Healthy
 Container sandrpod-poder     Healthy
 Container sandrpod-caddy     Healthy
```

### 2.5 签一个长得像 E2B 的 API key

E2B SDK 在客户端就按 `/^e2b_[0-9a-f]+$/` 校验 key，所以签发时就用这个形状，
可以直接当 `E2B_API_KEY` 用。服务端只存 hash，裸 key 只显示一次。

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

## 三、客户端只改两个环境变量

这就是客户端侧的全部改动。不用 fork，不用打补丁，没有 shim：

```bash
pip install e2b e2b-code-interpreter     # 官方包，原封不动
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

从一台笔记本经公网测的，所以下面这些数字包含真实往返时延，不只是服务端耗时：

| | 中位数 |
|---|---|
| `Sandbox.create()` → 可用 | **2.7 秒** |
| `commands.run` 往返 | **217 毫秒** |
| `run_code` 往返 | **194 毫秒** |

（冷镜像拉取后的第一次调用会慢很多，预留大约 20 秒，只有一次。）

---

## 四、逐个函数验一遍

`e2b` **2.35.0** 与 `e2b-code-interpreter` **2.9.0**，打上面那套部署。
下面每一行都真的执行过。

### 生命周期 —— 11/11

| 调用 | 结果 |
|---|---|
| `Sandbox.create()` | `e2b6c1b0f2b8a72dd40`，2.9 秒 |
| `is_running()` | `True` |
| `get_info()` | `state=running`，`envd_version=0.2.0`，cpu/内存有值 |
| `set_timeout(600)` | 正常——映射到沙箱的闲置 TTL |
| `Sandbox.list()` | 能找到 |
| `Sandbox.list(query=SandboxQuery(metadata={...}))` | 能找到——metadata 能往返也能过滤 |
| `Sandbox.connect(id)` | 重新接上 |
| `pause()` / 用 `connect()` 恢复 | `True`，然后 `echo back` → `back` |
| `kill()`，再 `is_running()` | `True`，然后 `False` |

### 文件系统 —— 11/11

`write` · `read` · `read(format="bytes")` · `exists` · `get_info` · `make_dir` ·
`list` · `rename` · `remove` · `write_files`（批量 multipart）· `watch_dir`

```
watch_dir  →  2 个事件: [('watched.txt', CREATE), ('watched.txt', WRITE)]
```

### 命令 —— 7/7

前台 `run` · 非零退出抛 `CommandExitException(exit_code=3)` ·
`run(background=True)` 返回真实 pid · `list` · `send_stdin` · `kill` ·
以及增量流式：

```
connect (streaming)  →  3 个独立分块，exit=0
```

是三个分块，不是一坨缓冲后的整体——这正是 Caddy 配置里 `flush_interval -1`
在保护的东西。

### PTY —— 1/1

`create` / `send_stdin` / `resize` / `kill`，一个真实的终端会话：

```
killed=True, 回显 'pty-works'=True, 16 帧
```

### 代码解释器 —— 14/14（run_code 5、有状态 2、context 5、图表 2）

有状态内核，这是这个 SDK 的核心价值：

```python
sbx.run_code("a = 100")
sbx.run_code("a * 2").text        # → '200'
```

context 是相互独立的命名空间，`restart` 清空一个而不销毁它：

```
create                 → 1bba3fa6bdceaa3e…，写入 z=7
list                   → 1 个 context
隔离性                 → context 内 z=7，默认 context 里 NameError
restart                → NameError: name 'z' is not defined
remove                 → 已删除
```

错误是结构化返回的，不是一段文本：

```
run_code("1/0")  →  ZeroDivisionError: division by zero
```

图表是被捕获的，不只是打印出来。沙箱内画的 `matplotlib` 图会以 PNG 形式出现在
`Execution.results[].png` 里，还带结构化的图表元数据：

```
matplotlib → PNG          19276 字节, 头部 b'\x89PNG'
图表元数据                BarChart
```

![在自托管沙箱里生成的 matplotlib 折线图](https://blog.sandrpod.com/e2b-compatible-sandbox-api/chart-from-sandbox.png)

### 端口与预览 —— 1/1

在沙箱里起个服务，问它的外部地址，然后打开：

```python
sbx.commands.run("cd /workspace && python3 -m http.server 8000 &", background=True)
host = sbx.get_host(8000)     # 8000-e2ba4581bfc832c3459.example.com
```

```
$ curl https://8000-e2ba4581bfc832c3459.example.com/index.html
<h1>served from the sandbox</h1>
```

不需要凭据，因为浏览器发不了——能猜到那个不可预测的主机名本身就是凭证，
E2B 也是这么设计的。同一个通配符域名下的 envd RPC 接口**不**在此列：

```
GET https://49983-<id>.example.com/files          → 401
GET https://49983-<id>.example.com/files + key    → 200
```

如果你这套部署里所有消费方都是能带 key 的程序，设
`SANDRPOD_E2B_PRIVATE_PORTS=1`，预览 URL 也会要求凭据。

### 指标 —— 1/1

```
get_metrics()  →  1 个采样, cpu=0%, mem=1044590592
```

---

## 五、这个过程炸出来的东西

把这套东西立起来，暴露了三个真实缺陷。它们此前一直没被发现，因为之前那轮
兼容性测试跑的是明文 HTTP 的调试监听端口——请求根本不经过
`<port>-<sandboxID>.<domain>` 这个 Host 路由，所以这三个问题一个都不可能出现。

- **`is_running()` 永远返回 `False`。** SDK 会去 envd 主机上探 `GET /health`，
  把 502 读成"没在运行"。而 `/health` 不是一条被路由的 envd 路径，于是它掉进
  通用端口代理，代理去拨容器内的 `127.0.0.1:49983`，发现没人监听，返回 502——
  对一个完全健康的沙箱。
- **代码解释器的 context 完全不可用**（401）。SDK 在 `/execute` 上发
  `X-Access-Token`，但在 `/contexts` 上**一个凭据都不发**——这种不对称只有在
  那个主机上真的开了鉴权之后才会暴露。
- **预览 URL 要求 API key**，而浏览器根本给不了。

三个都在 **v0.5.2** 修掉了，每个都配了回归测试，并且确认过把修复回滚后测试会
失败。这是"为什么要在真实域名上做这件事而不是在笔记本上"最诚实的论据：调试
监听端口只能告诉你协议实现了，只有通配符 Host 路由能告诉你它实现得**对**。

### 还没有的

如实列出来，因为在集成的时候才发现更糟：

| 调用 | 状态 |
|---|---|
| `create_snapshot()` | `405` —— 未实现 |
| `fork()` | `405` —— 未实现 |
| 创建时的 `envVars` | 接受，但真实 SDK 还没有走到这条路径 |

`get_mcp_url()` 和 `download_url()` 确实返回格式正确的 URL
（`50005-<id>.<domain>/mcp` 和 `49983-<id>.<domain>/files?path=…`）；这轮扫描
只验证了它们能被生成，没有验证每一个消费方都能用。

**总计 50 项检查，48 项通过。** 两项失败就是上面那两个 405。

---

## 六、长期养着它要付出什么

一台机器。这套栈空转时是四个容器；沙箱是按需创建的 Docker 容器，按你为每个
沙箱设的闲置 TTL（`set_timeout`）回收。证书续期是一个 systemd timer。Postgres
里存的是沙箱、任务、worker 和 API token 的 hash——备份它，其余的东西都能从
Compose 文件重建出来。

横向扩展是加 worker，不是把这台机器加大：每个 `poder` 通过自己拨出的隧道连
控制平面，所以第二台机器——另一个云、另一个区域、办公室 NAT 后面的一台盒子——
只要跑一个容器就加入了，防火墙一条入站规则都不用改。

对国内部署来说这条尤其实用：你可以把控制平面放在有公网 IP 的那台机器上，
把真正跑代码的 worker 放在内网、放在专有云、甚至放在客户自己的机房里，
数据始终没有离开那个网络边界。

---

## 自己跑一遍

上面所有东西——Compose 文件、Caddyfile、`tcdns.py`、lego 的 systemd 单元，
以及两轮测试扫描——都在[配套仓库](https://github.com/ChangjunZhao/examples/tree/main/example/e2b-compatible)里。
从一台空服务器到扫描通过，一共四个脚本。

- SandrPod：<https://github.com/sandrpod/sandrpod>（Apache-2.0）
- `pip install sandrpod-cli`

如果你换一个 DNS 服务商或者换一个发行版，最先该看的两个地方是通配符记录和
`Host` 头，顺序就是这个。这篇里其余的部分都很顺。
