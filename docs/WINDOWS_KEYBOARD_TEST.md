# Windows 键盘 Ctrl+C 测试

本测试验证根目录 `collaborate.py` 在本人按 Ctrl+C 后如何保留已完成的结果。
它使用合成工作线程，不登录客户端、不发送供应商请求、不使用真实密钥。
它不验证订阅客户端的取消行为，也不能证明远端已停止或没有产生费用。

需要 Windows 和 Python 3.10 或更新版本。在仓库根目录运行：

```powershell
python -B tools/windows_keyboard_interrupt.py --scenario normal
```

1. 找到新开的 **AI-COLLAB KEYBOARD TEST - normal** 窗口。
2. 等待 `READY`。只激活这个测试窗口，不选择其中的文字。
3. 本人在该窗口按一次 **Ctrl+C**。不要在原有任务窗口按键。
4. 程序自动检查结果；启动时显示的独立结果目录中会留下 `summary.json`。

再运行第二个场景，按相同步骤在新窗口按一次 Ctrl+C：

```powershell
python -B tools/windows_keyboard_interrupt.py --scenario startup_failure
```

最后运行无中断对照，**不要按键**：

```powershell
python -B tools/windows_keyboard_interrupt.py --scenario startup_failure_no_signal
```

| 场景 | 期望 |
| --- | --- |
| normal | 退出码 130；第一家已记录的中文结果保留；第二家未知且不重发 |
| startup_failure | 第二线程启动失败；按键后退出码 130；未完成结果未知且不重发 |
| startup_failure_no_signal | 退出码 1；等待第一家完成并保留结果；不启动第二家、不重发 |

脚本会创建新的专用控制台、核对控制台内的进程，并仅在必要时结束自己创建的
合成子进程。未在等待时间内按键会使该次检查失败，不会自动重试。
`--timeout` 可指定等待秒数，范围 5–300；`--output-root` 可指定结果目录的父目录。

`passed: true` 只表示捕获的退出码、输出、SIGINT 次数和调用次数满足检查。
SIGINT 本身不能辨别来自实体键盘还是其他程序；`keyboard_origin` 会保持
`requires_user_confirmation`，必须另外确认本人实际按键后才称为键盘测试。
保存源码哈希以对应被测版本；不要把未经检查的原始本机日志上传公开仓库。

CI 只运行验证器的离线测试，不会打开窗口或替使用者按键。
微软说明 Ctrl+C 通常传给共享该控制台的进程，因此不要在承载其他任务的窗口中尝试。
[微软控制台说明](https://learn.microsoft.com/en-us/windows/console/ctrl-c-and-ctrl-break-signals)

## 2026-11-13 本机验证记录

Windows / Python 3.12.14 下，使用者确认在两个专用测试窗口分别实际按了一次
Ctrl+C。两次记录均捕获一个 SIGINT，子进程退出码为 130，从信号处理到观察
进程退出均在五秒检查上限内。normal 保留已记录的中文结果；startup_failure
只调用第一个合成任务，两者均未重发。无按键对照退出码为 1，第一项结果保留。

被测 `collaborate.py` 来自 `358a9e4`，SHA-256 为
`0be9cb6df10952edc0b7e7ab552b7551d78bd047baeb9e8409e406023939affa`。
本轮未修改该生产文件，只增加可复用测试入口、验证器测试和说明。
首次后台启动的窗口未能让使用者找到，不能算键盘通过；后来显式使用可见
窗口启动设置后才完成上述两次人工按键。程序日志不能独立鉴别键盘来源，
“本人按键”依据使用者的明确确认。

这些测试没有发送真实供应商请求，不覆盖请求在服务端的取消或计费、反复按键、
所有终端宿主、系统重启以及订阅适配器的中断路径。两家协作者审查的是代码与
已运行输出，不是由它们操作键盘。
