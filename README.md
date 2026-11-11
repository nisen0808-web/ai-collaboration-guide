# AI 多模型协作：最小参考实现

把一个问题分别交给 Claude 和 Grok，再由你或主控 AI 对照、核验结果。程序只依赖 Python 标准库；任何具备终端执行能力的 AI 都可以帮助使用。

这份示例适合**会使用终端、愿意自行开通 API 的人**。它不是插件或一键安装包，也不会合并不同公司的订阅或额度。普通网页聊天不能仅凭仓库链接自动调用本机程序。

```text
使用者 → 主控 AI（自己分析、负责核验）
                    ↓ 相同问题，各自独立
               Claude API       xAI API
                    ↓              ↓
                各自结果 → 主控对照与交付
```

## 第一步：费用与隐私

示例使用 **Anthropic API 和 xAI API，可能产生按量费用**。已有的 Claude、Grok、ChatGPT 订阅不保证包含 API；请在各自账户确认模型、计费及额度。此示例不会复用订阅登录，也没有美元硬预算。

**密钥不要发送到聊天，也不要交给 AI、提交仓库或录入截图。** 有终端权限的 AI 可能读取同一进程、环境变量或同账户可访问的数据。把变量改名只避免特定计费冲突，**不是隔离**。如果不希望主控 AI 接触密钥，请本人在可信、独立的终端手动运行本程序，审查输出后再把需要的内容交给 AI；同一操作系统用户或管理员仍可能拥有访问能力，高安全要求需使用实际隔离环境。

程序使用专用名称 `COLLAB_ANTHROPIC_KEY` 和 `COLLAB_XAI_KEY`，不读取 `ANTHROPIC_API_KEY` 或 `XAI_API_KEY`。这避免本示例使用标准变量名触发 Claude Code 的认证优先级；但如果环境里本来已设置 `ANTHROPIC_API_KEY`，Claude Code 仍可能走 API 计费。请在启动 Claude Code 前自行确认其环境和 `/status`；不要在已设置标准 API 密钥的终端误启动订阅版 Claude Code。[官方说明](https://code.claude.com/docs/en/authentication)

发送任务即向所选供应商传输内容，请先确认有权分享。程序给 xAI Responses 请求传递 `store:false`，以关闭后续检索存储；这**不等于服务端零保留**。xAI 文档说明默认请求和响应仍可能为审计保存 30 天；零数据保留取决于单独条件与配置。[文本生成](https://docs.x.ai/developers/model-capabilities/text/generate-text) · [安全 FAQ](https://docs.x.ai/developers/faq/security)

## 第二步：准备环境

需要 Python 3.10 或更新版本、网络、两家 API 的本人凭据，以及各自账号支持的模型 ID。无需安装额外 Python 包：

- [Anthropic API](https://platform.claude.com/docs/en/api/overview) · [定价](https://platform.claude.com/docs/en/about-claude/pricing)
- [xAI 快速开始](https://docs.x.ai/developers/quickstart) · [模型](https://docs.x.ai/developers/models) · [定价](https://docs.x.ai/developers/pricing)

下载仓库，在其目录打开**本人控制的可信终端**。选择下面一种密钥方式，模型 ID 从各厂商官方列表和自己的账户确认。不要把真实密钥放进命令历史或交给主控 AI。

### 方式 A：本人交互输入（优先）

程序通过隐藏输入读取密钥，只在当前进程内使用，不持久化；无可用交互终端时会停止。模型名不是秘密，可设置在本终端中。

Windows PowerShell：

```powershell
$env:CLAUDE_MODEL = '<自己账户支持的模型 ID>'
$env:GROK_MODEL = '<自己账户支持的模型 ID>'
python collaborate.py --ask-keys --confirm-paid
```

macOS / Linux（Bash）：

```bash
export CLAUDE_MODEL='<自己账户支持的模型 ID>'
export GROK_MODEL='<自己账户支持的模型 ID>'
python3 collaborate.py --ask-keys --confirm-paid
```

程序会提示输入任务和密钥。Windows 可用 `py` 代替 `python`；请在提示时输入简短、无敏感信息的问题。

### 方式 B：专用会话变量

如需由脚本使用环境变量，使用 `COLLAB_ANTHROPIC_KEY`、`COLLAB_XAI_KEY`、`CLAUDE_MODEL` 和 `GROK_MODEL`，由本人在可信终端或受保护的密钥管理器设置；不要将密钥直接写入命令或 shell 历史。**同一进程及其子进程能读取这些变量**，所以若 AI 同样拥有终端访问权限，不能向使用者保证它无法读取密钥。

### 输入任务：交互输入或 UTF-8 文件

交互方式运行时，在提示后输入一行任务。较长的任务也可以先由本人用文本编辑器保存为 UTF-8 文件，然后指定该文件：

```sh
python collaborate.py --task-file task.txt --ask-keys --confirm-paid
```

`task.txt` 只是示例文件名；请勿将私密任务文件提交到公开仓库。程序严格按 UTF-8 读取，支持 UTF-8 BOM。任务最多 20,000 个字符；空任务、不能按 UTF-8 解码或超出上限时不发送请求。

Windows PowerShell 的管道可能在程序收到内容之前改变编码，PowerShell 5.1 尤其可能把中文替换成问号；一旦替换，程序无法恢复原文。Windows 下默认拒绝管道输入，优先使用交互输入或 `--task-file`。`--stdin-utf8` 仅供能独立确认整个管道已正确传递 UTF-8 原文的使用者显式声明；它不会检查或修复上游已经发生的替换。其他系统的管道也必须确保上游编码正确。

`--confirm-paid` 用于防止误触，**不是授权机制**：有命令执行能力的程序仍可自行添加该参数。只有在使用者已明确选择 API 及同意费用后，主控 AI 才能调用。发送前会同时检查两家的模型名及凭据格式；任一方本地配置缺失或格式无效时，两家均不发送。本地格式检查无法证明模型可用、密钥有效或账户有额度。

## 第三步：结果、超时与核验

每家最多发一次请求，无自动重试。默认 `--max-tokens 512`，可指定 `1–4096`；默认 `--timeout 60` 秒，可指定 `1–300`，例如 `--timeout 180`。这是网络操作超时而非整个任务的硬截止，也不是流式输出。慢响应有可能已计费而未收到结果；HTTP 408、5xx（包括 504）和网络中断都不能证明服务端没有执行，程序会标记 `submission_uncertain`，不要盲目重投。两家并行发出请求，等待结束后一次性输出；中途强制终止可能丢失已产生费用的结果。正常完成时，一家失败不会抹去另一家的结果。

`--max-tokens` 是请求中的输出 token 参数，**不是美元预算或总费用上限**。xAI 的推理 token 可能不计入可见输出限制，但仍可能计费；较低的输出限制也可能截断答案。实际计费以各自官方账单和账户为准。[xAI 文本模型建议](https://docs.x.ai/developers/model-capabilities/text/best-practices) · [xAI 用量与费用](https://docs.x.ai/developers/cost-tracking)

Claude 请求使用 Messages API，版本头 `anthropic-version: 2023-06-01`；xAI 使用 Responses API。每家可能返回的状态包括：

- `complete_unreviewed`：收到完整文本，尚须主控核验。
- `truncated`：识别到输出被截断，保留供应商已返回的文本和可用编号，但不能当成完整答案。
- `refused`：供应商拒绝，可能包含返回的说明；`incomplete`：其他尚未完成或不确定的响应。
- `invalid_response`：响应格式无效或不能解释；`not_submitted`：本地预检未通过，未发出请求。
- `failed`：供应商返回明确的失败响应；`submission_uncertain`：是否执行或计费无法确定。

可用时，输出会包含响应正文中的 `response_id`、HTTP 头中的 `request_id` 和供应商实际返回且符合格式检查的 `usage` 字段；缺失的数据不会被当成零。`cost_in_usd_ticks` 是 xAI 返回的原始单位，100 亿 ticks 等于 1 美元；不是所有响应都会提供，也不是最终账单。返回的模型名可用于核对，思考强度无法确认则标记 `not_verified`。[Claude 停止原因](https://platform.claude.com/docs/en/build-with-claude/handling-stop-reasons)

程序使用 ASCII 兼容的 JSON 输出，解析后可还原中文，避免旧版 Windows 输出编码无法表示的字符导致打印失败。对已知完整密钥的遮盖不能识别所有变形或部分片段；分享任何输出之前，请自行检查其中是否包含密钥、任务原文或其他敏感内容。

**示例输出**：运行 `python demo_offline.py` 可用假凭据和模拟网络复现，不调用真实供应商，也不是实测账单或真实作业记录。示例模拟一家成功、一家失败，因此进程退出码为 1：

```json
{
  "providers": {
    "claude": {"status": "complete_unreviewed", "output": "17 * 23 = 391", "effort": "not_verified"},
    "grok": {"status": "submission_uncertain", "reason": "transport_error", "retry": "not_attempted"}
  }
}
```

为便于阅读，此处只展示关键字段；完整 JSON 含程序实际返回的其他字段。第二家没有得到可核验结果，不能称为双家验证。

主控 AI 可使用这个核验指令：

> 请先独立完成原问题，再分别检查两份结果及其依据。逐条标注“三方一致”“有分歧”或“无法验证”，核对可查的原始来源与计算，解释分歧，并给出自己的结论与仍未解决的缺口。不得把模型自报或模拟结果当作真实来源。

## 不计费的离线测试

```sh
python -m unittest -v test_collaborate test_preflight test_api
```

测试使用模拟响应、假密钥和本机回环地址（用于测试重定向），不请求真实供应商。通过表示覆盖的本机逻辑通过；账号、模型权限、真实网络、真实输出和计费仍需使用者授权后实测。仓库的 GitHub Actions 配置会在支持的 Python 版本上运行相同的离线测试；以 Actions 的实际运行结果为准。

## 必须做到与进阶

**必须：** 只发送必要资料；不分享凭据；独立输入；不自动重试提交不明或失败的请求；如实报告缺失结果、输出截断和不可验证的信息。实际模型能力、权限、额度和费用以本人账户及官方记录为准。

**进阶：** 可在此基础上自行增加 MCP、任务持久化或其他模型；这些不属于当前示例。Codex 仍可作为**客户端**连接外部 MCP 服务。旧的 `codex mcp-server` 则已被移除；迁移路径是 Codex app server，它不是 MCP 的直接替代，并且官方称其目前为实验性方案。[Codex MCP 客户端](https://learn.chatgpt.com/docs/extend/mcp?surface=cli) · [移除与迁移说明](https://learn.chatgpt.com/docs/mcp-server)

Claude Code 和 Grok Build 均有官方非交互客户端，但账号权益、自动化与第三方产品条款需分别核验；本示例未实现订阅 CLI 适配。[Claude Code 条款](https://code.claude.com/docs/en/legal-and-compliance) · [Grok Build](https://docs.x.ai/build/overview)

## 删除与许可

关闭本终端可清除仅在会话中设置的变量；删除仓库文件即可移除本程序。已发送至供应商的数据按各自政策处理。代码采用 [MIT License](LICENSE)。
