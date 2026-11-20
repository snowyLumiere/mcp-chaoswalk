# MCP-ChaosWalk

面向隔离测试实例的实验性 MCP stdio 回归工具。客户端从显式批准的只读工具中选择调用，从输入 Schema 生成测试参数，并复用同一轮成功响应中的值。无需 LLM API。结果记录实际执行的调用、获成功响应的工具，以及有明确协议证据的异常。

## 安装

需要 Python `>=3.10`（依赖 MCP Python SDK `>=1.30.0,<2`）。

```bash
# 从 PyPI 安装
pip install mcp-chaoswalk

# 或使用 uv 安装独立 CLI 工具
uv tool install mcp-chaoswalk

# 从源码克隆并同步锁定依赖（开发与测试）
git clone https://github.com/SnowyLumiere/mcp-chaoswalk.git
cd mcp-chaoswalk
uv sync --locked --extra dev
```

安装后可通过 `mcp-chaoswalk`、`mcp-chaos-walk` 或 `python -m mcp_chaoswalk` 调用，并使用 `mcp-chaoswalk --version` 检查版本。

## 快速开始

已安装 CLI（包括仅安装 wheel 的环境）可以在任意工作目录运行自带的只读 fixture。示例只读取固定的内存记录。

```bash
mcp-chaoswalk --demo --steps 11 --walks 2 --output summary.md
```

在源码目录开发或运行测试时，使用锁定的依赖运行 CLI 与测试：

```bash
uv run --locked mcp-chaoswalk --demo --steps 11 --walks 2
uv run --locked pytest
```

可以用有限延时演示超时与重放：

```bash
mcp-chaoswalk --demo --read-only-tool get_slow_example --steps 1 --timeout 0.1
```

## 自有 MCP 测试实例

将启动程序指向专用的测试实例。运行前由操作者配置文件系统、网络和权限隔离，使用合成数据与只读后端权限。stdio 子进程具有其启动环境授予的权限。子进程继承 MCP SDK 的平台环境变量白名单，生产凭据应从测试环境和启动参数中移除。

```bash
mcp-chaoswalk --cmd './start-readonly-mcp-fixture' \
  --isolated-test-instance \
  --read-only-tool list_examples \
  --read-only-tool get_example \
  --steps 20 --walks 2 --output summary.md
```

`--isolated-test-instance` 记录操作者的确认，实际隔离由启动环境提供。重复指定 `--read-only-tool` 以批准多个工具。工具还需声明 `readOnlyHint: true`，设置 `destructiveHint: true` 的工具会被跳过。每轮建立新会话并重新初始化客户端值池，外部后端状态由操作者维护。收到工具目录变更通知后，客户端停止发起新调用，结束测试并标记覆盖不完整。自动重放和缩减仅用于自带的固定内存 fixture。

| 参数 | 作用 |
| --- | --- |
| `--demo` | 运行自带的只读内存 fixture（与 `--cmd` 二选一）。 |
| `--cmd` | 启动外部隔离 MCP stdio 测试实例的命令（与 `--demo` 二选一）。 |
| `--isolated-test-instance` | 确认外部目标已隔离且仅使用测试数据（使用 `--cmd` 时必填）。 |
| `--read-only-tool` | 批准一个只读测试工具，可重复指定（使用 `--cmd` 时至少指定一个）。 |
| `--steps` | 总工具调用预算，默认 100。 |
| `--walks` | stdio 会话轮数，默认 5。 |
| `--timeout` | 单次调用等待时间，默认 3 秒。超时后结束该会话。 |
| `--startup-timeout` | `initialize` 和 `tools/list` 各自的等待时间，默认 5 秒。 |
| `--seed` | 客户端伪随机数种子，默认 42。目标数据、工具目录顺序以及响应受目标实例控制。 |
| `--no-shrink` | 跳过自带 fixture 的重放及缩减。 |
| `--output` | 写入可分享的 Markdown 摘要，其中只有固定分类和统计。若文件已存在，启动前立即失败退出（除非指定 `--force`）。 |
| `--force` | 允许覆盖已存在的 `--output` Markdown 摘要文件（不影响 `--local-log`）。 |
| `--local-log` | 创建包含启动命令、原始参数、响应和诊断的私有 JSON 调用日志（权限 `0600`，拒绝覆盖已有文件并在启动前快速失败）。请保存在本地。 |
| `--version` | 输出版本号并退出。 |

## 理解结果

- `CALL_TIMEOUT` 表示调用超过客户端时间阈值。串行测试可以观测响应超时，判断并发死锁需要额外证据。
- `RPC_INTERNAL_ERROR` 表示收到 JSON-RPC `-32603`。工具 `isError` 和普通 RPC 错误单独计数。连接中断或会话调用异常记录为 `SESSION_ERROR`。业务流程正确性由特定服务的回归断言验证。
- `SCHEMA_VIOLATION` 表示响应缺少所需的结构化内容、与声明不符，或声明的输出 Schema 无效。Schema 引用无法在本地解析时，调用记为 `SCHEMA_UNVERIFIED` 并标记覆盖不完整。校验使用文档中的引用和自带的元 Schema，当前会话继续执行已批准的调用。
- 客户端保留 MCP `structuredContent` 和文本内容，优先使用结构化值支持后续调用。输入生成支持简单类型和枚举，遇到分页工具目录会结束测试并报告覆盖不完整。引用、联合类型、嵌套约束等未支持的输入 Schema 会被跳过，并计入覆盖缺口。
- 重放报告区分原始观测、未确认重放和已确认重放。缩减只接受与原观测指纹一致的重放证据。JSON 私有日志用于人工审阅，字段是调用记录和测试结果。

退出码 `0` 表示完成计划中的调用预算、每个批准工具至少成功一次，且没有覆盖缺口和异常观测。退出码 `1` 表示观测到异常，报告也会显示覆盖缺口。退出码 `2` 表示覆盖不足且无异常观测，或执行、配置、输出失败。工具级调用覆盖不代表输入空间或业务状态空间覆盖。

## 许可证

本项目采用 [MIT License](https://github.com/SnowyLumiere/mcp-chaoswalk/blob/main/LICENSE)（SPDX 标识符 `MIT`）。完整许可条款见 `LICENSE`。
