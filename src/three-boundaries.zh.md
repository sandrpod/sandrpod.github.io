# 三道边界，和一把永远进不了沙箱的钥匙

*0.5.x 隔离的是一个文件系统。0.6.0 问的是更难的问题：如果代码压根不走你造的那扇门呢？*

---

每一种沙箱设计，洞都在同一个地方。你在文件 API 前面立了道门，模型直接调
`open(2)`；你在网络前面放了代理，模型把 `HTTP_PROXY` 一删。你运行的这段代码是
模型替你写的，它没有义务走你的门。

SandrPod 0.5.x 的文件边界其实做得不错：toolbox 会把每个路径对一遍策略，hardlock
清单让 `~/.ssh` 够不着。它也很诚实地写了那句话——*任意代码执行本身不是文件 API
能关住的东西*——然后就停在那儿了。这次把它补完，靠的不是把门做得更聪明，而是在门
**下面**加三层，每一层都管上一层看不见的那部分。

## 第一层：你能去哪儿

出网白名单本身不稀奇，有意思的是它有三种持法，而且**失败方式各不相同**：

| 模式 | 规则在哪 | 收紧的规则何时生效 | 服务端连不上 |
|---|---|---|---|
| `-egress-mode=enforce` | 本地文件 | 重启时 | 照常 |
| `-egress-mode=server` | 两端（轮询副本） | `-egress-sync` 内（60s） | 用最后一份副本 |
| `-egress-mode=decider` | 只在服务端 | 下一个连接（≤10s 缓存） | **拒绝** |

第三种存在的理由很朴素：一份你没法快速撤回的策略，等于没有。代价写在明面上而不是
塞进脚注：问不到答案的 decider 就是拒绝。连不上策略服务绝不能悄悄变成开放代理，
而"我们没问到规则"和"规则说不"是两件事——代理有意把它们的报错区分开。

清单底下有个值得
从 [sandbox-runtime](https://github.com/anthropics/sandbox-runtime) 借走的细节：
域名只解析**一次**，校验解析出来的地址（loopback、link-local、云元数据、本机自己的
网卡），然后拨的是校验过的那个 IP。第二次 DNS 查询正是 DNS 重绑定攻击住的地方。

容器沙箱用另一套机制拿到同一份策略——因为上面这些对"无视代理环境变量"的进程毫无
作用：每个 pod 一个 `Internal: true` 的网络，加一个 `egress-proxy` sidecar，容器除了
代理之外没有任何路由。两种部署，一份策略文件，"这台主机能不能出去"只有一个答案。

## 第二层：内核，管那些不听你的进程

第二层是模型绕不过去的那层，因为它不是一个 API。打开 `-os-sandbox` 后，toolbox
派生的每个进程都跑在 bubblewrap（Linux）或 Seatbelt（macOS）里，形状一致：

- **根只读**，"把钥匙写到以后能读到的地方"无处落地；
- **hardlock 被遮蔽**——`~/.ssh` 不是"文件 API 不让读"，是**根本不在**；
- **持久化路径只读**——`.git/hooks`、`.git/config`、`.bashrc`、`.mcp.json`、
  IDE 启动配置。这些文件在 agent 工作树里唯一合理的用途，就是等沙箱没了之后再跑一次；
- **能力被丢掉**；Linux 上还有**独立的 network namespace**，唯一出口是代理——经由
  loopback 上的 relay，因为没有桥接的 namespace 连代理也够不着。

网络这块试了两次。第一版把沙箱放进独立 namespace 就不管了，那是一堵没有门的墙：
`wget` 连代理都解析不到。relay 就是那扇门——几十行代码，在 namespace 里监听
`127.0.0.1`，转发到宿主上代理的 unix socket。它自己**不带任何策略**，这是刻意的：
每个字节仍然交给代理，决定权在那里。

丢能力这件事有个更细的判断。`--cap-drop ALL` 是对的，直到 agent 以 root 运行——系统
服务就是这样跑的——然后它砸掉了"服务某个用户的 home 目录"这个最普通的场景：没有
`CAP_DAC_OVERRIDE` 的 root 读不了别人的文件。所以这一版只留两枚，
`CAP_DAC_OVERRIDE` 和 `CAP_DAC_READ_SEARCH`；`CAP_SYS_ADMIN` 仍然拿掉，因为它才是
能 `umount` 掉上面所有遮蔽的那一枚。

## 第三层：agent 永远拿不到的那把钥匙

第三层让我重写过两次文档。

能调用付费 API 的 agent 需要钥匙。钥匙在它的环境变量里，它就能打印、发到策略允许的
主机、粘进仓库。于是 `-credential-mask` 把真值换成 `fake_value_<uuid4>` 放进沙箱，
由代理在它**终止的 TLS** 上换回来。

"终止的 TLS"这几个字，就是诚实要开始的地方——TLS 终止不是中性操作。对被终止的主机，
沙箱看到的证书是**我们**签的；证书 pinning 失效，到上游的 mTLS 不可能工作。换来的
是"用这把钥匙，但别让模型拷走"目前唯一通用的答案：真实字节只存在于代理的内存里、
以及代理发出的那个请求里，沙箱够不到的地方。需要真正端到端通道的主机放进
`-credential-terminate-exclude`，代价是那些主机没有掩码——这是交换，不是疏漏。明文
HTTP 上带 sentinel 的请求一律拒绝，而不是放出去。

两条实现细节，因为这类特性通常烂在这两处。请求体替换是**等长**的，`Content-Length`
保持有效，代理不需要重新分帧。AWS SigV4 请求要用真 secret **重签**，因为签名是对整个
请求的 HMAC：`Credential` 作用域里的 sentinel 不是能原地换掉的值。重签器拿 AWS 官方
示例签名做过校验——重签错一点点，报错和"密钥错了"完全一样，这种 bug 能藏好几个月。

## 拒绝有两个读者

有个小改动，实际影响比预期大：每个拒绝现在带两份说明。

```
Reason:      路径 "/Users/me/.ssh/id_rsa" 被 hardlock 锁定（可用 sandrpod-tray unlock 解锁）
ModelReason: /Users/me/.ssh/id_rsa is hard-locked by the operator's policy and cannot be
             read or written; do not retry, and ask the user to unlock it in the tray if
             this is genuinely needed
```

第一份给对话框里的人看。第二份给模型看，随工具返回值进到对话里——一行英文，写明拒了
什么、接下来怎么办。两份都进审计。这个拆分不是装饰：把本地化的 UI 文案丢给模型，是
它无法行动的噪音；把英文祈使句塞进同意对话框，是让人点按钮时觉得"这是个 bug"。

## 集群必须和你意见一致

有墙的沙箱，前提是跑它的那台机器真能执行这堵墙。这次发布之前的 worker 完全不认策略
字段：容器照常起、状态报 `RUNNING`，而 API 还会把策略原样返回，仿佛它正在生效。

所以 worker 现在在隧道握手时声明自己能强制什么，控制面拒绝把策略放到一个没有声明该
能力的 worker 上：

```
409 worker capability unavailable: this sandbox asks for an egress policy, but
1 online local worker(s) do not support it (upgrade the poder to a build that
advertises "egress", or drop the policy)
```

agent 那边是镜像的逻辑：`-egress-mode=server|decider` 启动前先探测
`GET /api/v1/capabilities`，对不支持的 server 直接拒绝启动，而不是跑成 deny-all 让人
以为是网络故障。一条你专门为探测而造的路由返回 404 是无歧义的；沙箱记录的 404 不是。
这个区别就是整个设计。

## 这些测试到底付出了什么代价

上面是设计。真正有意思的是 bug，它们有一个共同点：每一个对"拥有它的那层"都是隐形的。

- **OS 沙箱从来没真正跑起来过。** `Resolve` 返回的是裸名 `bwrap`，toolbox 把它赋给
  `exec.Cmd.Path`，而 `execve` 不做 `PATH` 查找，于是每次 `Execute` 都死在
  `fork/exec bwrap: no such file or directory`。包自己的测试是过的，因为它们调
  `Command()` 再把结果交给 `exec.Command`，后者自己会查。这个问题是手工走查第一条
  命令时发现的。
- **交互式 PTY 完全没套沙箱。** `ProcManager.Start` 在 PTY 分支里提前 return，跳过了
  包装那一步——一个交互会话直接跑在 bubblewrap 外面，正是这一层存在的理由。
- **掩码后的 SigV4 从来没被重签。** 头替换先把 `Credential` 换成真 access key，重签器
  再去找 sentinel 就找不到了，请求带着"签名对不上密钥"发出去。每一次掩码的 AWS 调用
  都会以 `SignatureDoesNotMatch` 失败。
- **被 hardlock 的目录里的掩码文件根本不可见。** 替换挂载排在 hardlock 遮蔽之前，而
  默认种子把 `~/.aws` 整个遮掉——于是文档里那个例子产出一个**没有任何凭据文件**的沙箱。
- **继承的 `NO_PROXY` 让代理失效。** 子环境是 agent 的环境加上我们追加的，启动 shell
  里的 `NO_PROXY` 活了下来，所有认代理的客户端直接绕过。
- **测试脚手架在 CI 上跳过了自己，还报绿。** 夜检的 legacy 阶段用
  `if ! server & then` 启动，而后台启动永远成功——取反永远为真，于是每次运行都打印
  "skipping"。本地是过的，因为我手动跑过那条分支。这一条是关于脚手架的，也是整组里
  最有教育意义的：**带原因的 skip 仍然是 skip，只有真实路径能证明任何事。**

这些没有一个是单测发现的，而它们现在每个都有单测——但测试是在失败**之后**补的，对这
类 bug 来说这是唯一有效的顺序。发现它们的是把产品拼起来跑：一个带 bubblewrap 的特权
容器、一个真 agent、一个真控制面、一个 CLI，和一个会报告"我收到了什么"的上游 TLS
服务。这个脚手架（`hack/playground/run.sh`）现在每晚跑，头几次跑出四个 bug，后来又
出了两个。

## 升级，以及唯一要紧的那一步

先 worker，再控制面，最后 agent。只有第一步是强制的：0.5 的 worker 忽略策略字段，而
这个控制面会回 `409`，而不是造出一个看起来有墙的沙箱。**不带策略**的沙箱仍然可以调度
到旧 worker 上，所以分批滚没问题。

迁移是加列（两张表各一个 `ADD COLUMN … DEFAULT ''`），所以旧二进制仍然能读新二进制
迁移过的库。这次发布的所有能力默认关闭：不动参数升级，行为与之前完全一致，包括 agent
那条路径。

对 import `pkg/…` 的人有三处 Go 层破坏：`permission.AuditSink.Record` 多了一个参数，
`PoderRepository` 多了 `SelectBestFor`，`egress.Server.SOCKSURL()` 在 unix socket 模式
下返回 `""`。HTTP API、wire format、CLI 都是纯新增。

## 还没做的事

- **macOS 的 Seatbelt 没有验证过。** 在一个本身已被沙箱化的 shell 里没法应用这份
  profile，而 MDM 管着的开发者机器恰好就是这种情况。所以它挂在自己的探测后面，文档里
  标 experimental。Linux 那边是对真实沙箱化进程验过的。
- **写保护路径是挂载，不是安全模型。** 硬链接到受保护文件写的是同一个 inode；同一条
  命令里先建 `.git/hooks` 再写进去也仍然能成——保护从下一次 spawn 生效。堵这两个要
  Landlock。
- **终止隧道只谈 HTTP/1.1**，而且掩码不管上游把东西回显回来。
- **审计目前是日志，不是告警系统。** 它记录了哪个凭据去了哪个主机、哪条连接的 TLS 被
  终止；但这些停止发生时，没有人会收到告警。

发布在这里：[v0.6.0](https://github.com/sandrpod/sandrpod/releases/tag/v0.6.0)，
Linux / macOS / Windows 二进制，amd64 与 arm64 镜像，以及
[发布说明](https://github.com/sandrpod/sandrpod/blob/main/docs/RELEASE_NOTES.md)——同样
的升级顺序、同样的边界。如果你试了之后觉得哪里读起来不对，各种失败形态都写在
[OS_SANDBOX.md](https://github.com/sandrpod/sandrpod/blob/main/docs/OS_SANDBOX.md)、
[EGRESS_POLICY.md](https://github.com/sandrpod/sandrpod/blob/main/docs/EGRESS_POLICY.md)
和
[CREDENTIALS.md](https://github.com/sandrpod/sandrpod/blob/main/docs/CREDENTIALS.md)
里——包括还没能工作的那部分。