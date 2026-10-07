# commandcode-go-proxy

把 [Command Code](https://commandcode.ai)（含 $1/月 Go 套餐）的订阅额度反代为 **OpenAI 兼容**端点（`/v1/chat/completions`），自带中文用量看板。单文件、纯 Python 标准库、零依赖。

> ⚠️ **免责声明**：非官方逆向工程产物，与 Command Code / Langbase 无关联。使用逆向协议可能违反其服务条款，账号风险自行承担，仅供学习研究与个人合法订阅使用。禁止转售或多用户共用。

## 工作原理

Go 套餐没有官方 API（`/provider/v1/*` 返回 `upgrade_required`），但官方 CLI 每轮都调用 `POST https://api.commandcode.ai/alpha/generate`，该端点不限套餐。本代理把 OpenAI 格式请求翻译成该端点的信封格式（Vercel AI SDK `ModelMessage[]` + schema 严格的 `config`），把 NDJSON 响应流转回 OpenAI SSE/JSON。

```
OpenAI 客户端 ──POST /v1/chat/completions──▶ 本代理 ──翻译信封──▶ api.commandcode.ai/alpha/generate
   ◀──OpenAI SSE/JSON──  NDJSON→OpenAI 转换  ◀──NDJSON 流──
```

## 快速开始

```bash
# 本机
set CMD_CODE_TOKEN=user_xxxx        # Windows（token 见 commandcode.ai/settings/billing）
export CMD_CODE_TOKEN=user_xxxx     # Linux/macOS
python proxy.py
# 看板: http://127.0.0.1:18787/dashboard
```

```bash
# VPS 一键部署（Ubuntu/Debian，无需 clone，systemd 自启 + 冒烟验证）:
curl -fsSL https://raw.githubusercontent.com/Nice9z/commandcode-go-proxy/main/deploy.sh | bash
# 脚本依次要你输入: user_ token（手动粘贴）→ 网关 key（回车自动生成）
# → 端口（回车 18787）→ 默认模型（回车 GLM-5.3 Flash），其余全自动
# 国内网络下载慢会自动走 ghfast.top 镜像
```

手动方式（任何 Linux）：

```bash
CMD_CODE_TOKEN=user_xxx CMD_CODE_KEY=强随机密码 CMD_CODE_HOST=0.0.0.0 python3 proxy.py
```

客户端接入：Base URL `http://<host>:18787/v1`，API Key 填 `CMD_CODE_KEY`（或直接传 `user_*` token，直通模式）。

## 更新

脚本持续迭代，VPS 上升级只需一条命令（保留你的 token/网关 key 配置，只替换程序并重启）：

```bash
curl -fsSL https://raw.githubusercontent.com/Nice9z/commandcode-go-proxy/main/deploy.sh | bash -s -- update
```

或重新完整跑一遍 `deploy.sh`（会保留 `/etc/cc-go-proxy.env` 里的配置重新写入）。

## 环境变量

| 变量 | 默认 | 说明 |
|---|---|---|
| `CMD_CODE_TOKEN` | 空 | 上游 `user_*` token（服务端保密；不设则要求客户端 Bearer 直传） |
| `CMD_CODE_DEFAULT_MODEL` | `z-ai/glm-5.3-flash` | 客户端未指定模型时使用（Go 计划主力：1M 上下文 / $0.15/$0.50） |
| `CMD_CODE_KEY` | 空 | 网关 key，客户端须以 `Bearer` 或 `?key=` 传入；**公网部署必设** |
| `CMD_CODE_HOST` / `CMD_CODE_PORT` | `127.0.0.1` / `18787` | 监听地址/端口 |
| `CMD_CODE_VERSION` | `1.53.1` | `x-command-code-version`（钉在已实现的协议形状上，勿盲目跟 npm 最新） |
| `CMD_CODE_PROJECT_SLUG` | `cc-proxy` | `x-project-slug` 头 |
| `CMD_CODE_DB` | 脚本旁 `cc_proxy_usage.db` | SQLite 用量库 |
| `CMD_CODE_UPSTREAM` | `https://api.commandcode.ai` | 上游地址 |
| `CMD_CODE_UPSTREAM_RETRY_MAX` | `2` | 首字节前的透明重试次数 |
| `CMD_CODE_MAX_INFLIGHT` | `4` | 全局在途请求上限（0=不限），超限返回 503+Retry-After |

## 功能

- OpenAI 兼容：流式（SSE）/ 非流式、tool calls、系统提示词、多模态图片（data URL）
- 思考内容映射为 `reasoning_content`（DeepSeek 风格）
- 中文看板：请求数/成功率/输入输出缓存推理 tokens/**上游真实美元成本**（取自上游 `gateway.cost`）/延迟，按模型矩阵 + 最近请求表，今天/24h/7d/全部筛选，5 秒自动刷新
- SQLite 持久化（纯标准库 `sqlite3`），重启不丢
- 上游真实 401/402/403/429 语义错误原样回传，客户端能正确处理额度/鉴权问题

## 隐私与数据安全

**上行（离开你机器的只有这些）：**
- 翻译后的聊天信封本身（提示词文本——这是服务本体，不可避免）+ 必要 headers
- 会话 ID：由**上游 key 的 SHA-256** 派生（绝不从消息内容派生），12 小时轮换 + 确定性抖动——与官方 CLI 的长会话行为一致，且无法跨桶串联对话
- 不发送：文件路径、主机名、真实设备信息、客户端 IP、客户端 headers

**本地：**
- SQLite 只存：模型名、token 数、成本、延迟、状态码、**脱敏后**的错误文本。绝不存消息正文、绝不存在任何 key
- stderr 日志全部脱敏（`user_*`/`sk-*` 掩码），请求体永不落日志
- 每次请求的临时文件用后即删；数据库文件 chmod 600（Windows 上尽力而为）
- 上游 token 只存在于环境变量（或客户端 Bearer 直通），从不写盘

## 防封措施

- 协议版本钉在 **1.53.1**（与实现的 wire 形状自洽）——"自称最新版却说旧方言"比版本旧更可疑；升级前先核对 CLI 包再 bump
- 每 key 稳定会话 + `x-project-slug` 头，对齐真实 CLI 流量形态
- 空 system 发空格占位：阻止上游注入 ~7.5K token 默认提示词（白烧额度且形态异常）
- 在途并发上限（默认 4）：个人 CLI 使用不会同时打几十个请求
- 首字节前的传输层闪断才透明重试（2 次、400ms 退避）；语义错误（401/402/403/429）绝不重试；已向客户端吐字后绝不重试
- 饱和时对客户端 503 + Retry-After，让 SDK 自己退避
- **仍请保持人类式使用频率**：这是个人工具，不是农场。极高并发会触发风控

## 已知限制

- 上游高峰期可能掐断连接：未吐字前自动重试已覆盖；吐字后中断会报 502
- `x-command-code-version` 过旧被上游拒绝时，查当前 CLI 版本（`npm ls -g command-code`）后设 `CMD_CODE_VERSION` 重启
- Windows 下依赖 `curl.exe`（系统自带）；Linux/macOS 用系统 curl
- Cloudflare 按 TLS 指纹拦截 python-urllib（403 error 1010），故上游请求经 curl 子进程发出

## 协议来源与致谢

- [safzanpirani/pi-commandcode-provider](https://github.com/safzanpirani/pi-commandcode-provider) — wire protocol 逆向文档
- [MAXeaglet/commandcode-proxy](https://github.com/MAXeaglet/commandcode-proxy)（MIT）— 空系统占位、版本纪律、会话稳定性、重试边界等对策的参考
- [nasrulhadi/proxy-commandcode](https://github.com/nasrulhadi/proxy-commandcode) / [learningdog1/CommandCodeGo-manager](https://github.com/learningdog1/CommandCodeGo-manager) — 实现参考

## License

MIT
