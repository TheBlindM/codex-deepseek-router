# Codex DeepSeek Router 修改说明与上游 Issue 草稿

## 文档用途

本文档整理当前本地修改版 `codex-deepseek-router` 的：

- 已实施的代码与文档修改。
- 修改过程中实际遇到的问题。
- 每个问题的原因、解决方式和验证结果。
- 本地安装、Hook 与 Codex Desktop 排障记录。
- 一份可直接审核后提交到 GitHub 的中文 Issue 草案。

本文档只说明当前已验证的修改，不代表这些修改已经合并到上游。

## 来源说明

代码与文字由 AI 辅助整理，并交给仓库所有者审核和修改。仓库所有者负责最终保留范围、实现细节和提交决定。

## 一、当前修改概览

当前修改涉及 14 个已跟踪文件：

```text
14 files changed
285 insertions
23 deletions
```

主要分为三个主题：

1. Windows PowerShell staging 的确定性 bug。
2. Desktop 沙箱环境下 `direct_codex` 的临时 `CODEX_HOME` 隔离。
3. Flash-first 路由和 Pro 升级后的责任边界。

## 二、核心问题与修复

### 问题 1：Windows staging 未初始化 `$Agent`

#### 现象

在 Windows 上使用普通参数形式执行 staging：

```powershell
'probe' | powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass `
  -File hooks\plaintext-handoff.ps1 -Mode stage `
  -AgentType deepseek_flash -Policy FAST -Modality TEXT_ONLY `
  -StateDirectory C:\temp\codex-deepseek-handoff
```

会失败：

```text
The variable '$Agent' cannot be retrieved because it has not been set.
```

同一脚本的 JSON envelope 路径能工作，因为 JSON 分支会读取：

```powershell
$Agent = [string](Get-JsonProperty $envelope "agent_type")
```

普通参数路径读取的是参数 `$AgentType`，但没有把值赋给后续函数使用的 `$Agent`。

#### 修复

在 `hooks/plaintext-handoff.ps1` 的非 JSON 分支增加：

```powershell
} else {
    $Agent = $AgentType
}
```

这样后续角色校验、状态锁和 pending 文件名都使用同一个经过 ValidateSet 校验的值。

#### 回归测试

在 `tests/test_handoff.py` 增加 Windows-only 测试：

```text
test_windows_powershell_stage_hook_roundtrip
```

测试覆盖：

- 普通 `-AgentType` staging。
- pending 状态文件创建。
- `SubagentStart` Hook 投递。
- 子任务上下文内容。
- pending/claimed 状态清理。

### 问题 2：`direct_codex` 在主 `CODEX_HOME` 只读时无法初始化

#### 现象

Codex Desktop 可能使用受限身份运行 shell 工具。该身份能够读取主 `$CODEX_HOME`，但不能创建 Router 或嵌套 Codex 所需的锁、临时目录和状态数据库。

历史排障中先后出现过：

- 无法创建 `deepseek-router/manager.lock`。
- 无法创建 `.codex/tmp/arg0-windows`。
- 嵌套 Codex 无法初始化主状态数据库。
- 中文 Windows 下子进程输出被 GBK 解码产生 `UnicodeDecodeError`。

这些问题容易表现为“Router 不可用”，但根因是执行环境权限或编码，而非 DeepSeek 供应商配置失败。

#### 修复

`scripts/codex_deepseek_router.py` 中：

1. `_smoke_env()` 增加可选 `codex_home` 参数。
2. `direct_codex_delegate()` 每次运行都创建临时目录：

```python
with tempfile.TemporaryDirectory(
    prefix=f"codex-deepseek-router-{role}-"
) as runtime_home:
```

3. 子进程通过：

```python
env=_smoke_env(paths, Path(runtime_home))
```

使用这个临时目录作为 `CODEX_HOME`。

#### 效果

- 子执行拥有可写的独立状态目录。
- 不替换父模型、供应商、登录信息、配置或持久会话。
- 临时目录在任务结束后自动删除。
- 不再要求宿主工具身份写入主 `.codex` 状态数据库。

#### 测试

- `tests/test_security.py` 验证 `_smoke_env()` 可以使用独立 `CODEX_HOME`。
- `tests/test_plugin_runtime.py` 为客户端测试设置独立临时 `CODEX_HOME`。
- 本地真实调用分别验证 Flash 和 Pro 返回正确模型标记。

### 问题 3：大任务容易过早交给 Pro

#### 设计问题

仅根据“文件多、上下文长或任务看起来复杂”就升级 Pro，会让大量本来可以由 Flash 完成的只读调查产生更高成本。

同时，Pro 被升级后由谁负责实现、测试和验收，也需要明确，避免 Flash 父代理在 Pro 完成后用较弱推理覆盖 Pro 的技术判断。

#### 修复

`runtime/router.py` 将文本 fallback 的 Pro demand 阈值从 `0.55` 提高到 `0.80`：

```python
FLASH_FIRST_PRO_DEMAND_THRESHOLD = 0.80
```

当前自动分数公式：

```text
分数 =
  文件数 / 12       × 0.25
+ 上下文字符数 / 240000 × 0.35
+ 推理深度 / 10      × 0.40
```

边界：

- 分数 `< 0.80`：Flash。
- 分数 `>= 0.80`：Pro。
- 显式 `flash` 或 `pro` 模式不经过该阈值。

该阈值只控制 `runtime/cli.py --mode auto` 的纯文本 fallback，不控制显式 `delegate --agent-type ...`。

#### 路由规则

Flash 默认处理：

- 仓库搜索与代码地图。
- 多文件依赖梳理。
- 日志和测试输出分析。
- 文档读取与信息抽取。
- 有边界的根因调查。
- 只读修改提案和测试计划。

Pro 用于：

- 需要实际修改工作区。
- Flash 明确返回 `ESCALATE_TO_PRO` 或未满足验收标准。
- 并发、分布式不变量、安全边界、冲突证据或高回归风险。
- 无法由 Flash 现有证据收敛的系统级决策。

一旦 Pro 对升级任务负责，Pro 应负责该范围的实现、测试和验收。父代理负责机械事实检查，不确定的技术问题应返回 Pro，而不是由较弱模型自行覆盖。

## 三、文档与测试同步

与代码同步修改：

- `README.md`
- `README.en.md`
- `SKILL.md`
- `skills/use-deepseek-router/SKILL.md`
- `references/routing-policy.md`
- `references/compatibility.md`
- `docs/design-decisions.md`

新增或更新测试：

- `tests/test_handoff.py`
- `tests/test_plugin_runtime.py`
- `tests/test_security.py`
- `tests/test_acceptance_contract.py`

## 四、验证记录

最终在独立 D 盘仓库副本中，使用可写的临时 pytest 目录重新执行：

```text
239 passed, 20 skipped
9 security tests passed
compileall: OK
git diff --check: clean
```

第一次在 D 盘执行完整测试时，系统默认 `%TEMP%\pytest-of-*` 目录权限不可读，造成 fixture 初始化错误。把 `TMP` 和 `TEMP` 指向工作区可写目录后，全套测试通过，因此该失败不属于代码回归。

真实模型调用：

```text
deepseek_flash    completed    FLASH_VERIFY 391
deepseek-v4-pro   completed    PRO_VERIFY 391
```

## 五、本地安装与 Hook 排障记录

这些内容属于本机安装和 Codex 运行时问题，不建议直接写进核心代码 PR：

1. 本地 Marketplace 首次安装后，Plugin 缓存和源码版本需要逐文件核对。
2. Hook 信任由 Codex 管理，修改 Hook 内容或插件元数据后需要重新审核。
3. 插件缓存缺少 `.agents/plugins/marketplace.json` 时，`/hooks` 可能显示所有事件为 `0`。
4. 缓存中的 `.git` 和 `.pytest_cache` 若带拒绝 ACL，会使 Codex 无法计算插件内容指纹。
5. 中文 Windows 下 Router 的嵌套 Codex 输出需要 UTF-8 环境，可用：

```powershell
$env:PYTHONUTF8 = '1'
$env:PYTHONIOENCODING = 'utf-8'
```

6. `preferred_auth_method` 在当前 Codex 版本中已被忽略，属于废弃配置；不应把它当作 Router 故障。
7. 配置重置可能移除 Marketplace、Plugin 和模型目录；此时需要重新注册并执行 Router setup。
8. API Key 不应写入 TOML 或命令参数，应使用系统凭据或环境变量。

## 六、已知剩余事项

- 当前本地 `models.json` 与 Router 期望的完整模型目录元数据不完全一致，因此 `status` 可能显示 `partial`；这不影响已验证的 `direct_codex` 委派。
- 插件内容变化后，Codex 可能重新要求 Hook 信任。
- Hook 信任、模型目录注册和上游代码修复属于不同层次的问题，不应混为同一个失败原因。

## 七、可直接审核的中文 Issue 草案

### 标题

完善 Windows/Desktop 路由可靠性并明确 Flash-first 执行边界

### 正文

当前 Router 在 Windows 和 Codex Desktop 沙箱环境中存在两个可复现问题，同时 Flash/Pro 的自动路由和责任边界需要与设计说明保持一致。

#### 1. Windows 普通 staging 未初始化 `$Agent`

使用 `plaintext-handoff.ps1 -Mode stage -AgentType ...` 时会报：

```text
The variable '$Agent' cannot be retrieved because it has not been set.
```

原因是普通参数分支没有把 `$AgentType` 赋给后续逻辑使用的 `$Agent`。JSON envelope 路径已经正确赋值，所以只有普通参数路径失败。

建议在非 JSON 分支增加：

```powershell
$Agent = $AgentType
```

并增加 Windows staging → pending → Hook 投递 → 清理的回归测试。

#### 2. Desktop 沙箱下 `direct_codex` 可能无法写入主 Codex 状态

Codex Desktop 可能以只读身份访问主 `$CODEX_HOME`。即使 Router 配置、供应商和凭据都有效，嵌套子执行仍可能无法创建锁、临时目录或状态数据库。

建议每次 `direct_codex` 委派使用临时、可写的独立 `CODEX_HOME`：

- 临时目录仅用于该次子执行。
- 执行完成后删除。
- 不修改父模型、供应商、登录信息或持久会话。

#### 3. Flash-first 和责任边界需要明确

大上下文、多文件搜索、长日志和初步根因分析不应仅因规模较大就交给 Pro。

建议：

- Flash 默认负责有边界的只读调查和前实现分析。
- Pro 用于工作区修改、Flash 明确升级或失败、高风险推理，以及无法由 Flash 证据收敛的系统级决策。
- Pro 升级后负责该范围的技术实现、测试和验收。
- 父代理检查可机械验证的事实；未解决的技术问题返回 Pro。
- 文本 fallback 的 Pro demand 阈值从 `0.55` 提高到 `0.80`。
- 显式 Flash/Pro 选择保持不变。

#### 验证结果

```text
239 passed, 20 skipped
9 security tests passed
compileall: OK
git diff --check: clean
```

实际委派：

```text
deepseek_flash    completed
deepseek-v4-pro   completed
```

#### 风险

- 阈值调整会使部分原来自动选择 Pro 的任务改选 Flash。
- 临时 `CODEX_HOME` 以持久子状态换取 Desktop 沙箱兼容性。
- Windows PowerShell 回归测试仅在 Windows 执行，跨平台静态协议测试继续保留。

#### 来源说明

本实现由 AI 辅助完成，并交由仓库所有者审核和修改。仓库所有者负责最终保留的修改、审核过程及是否提交给上游维护者。

## 八、审核检查表

- [ ] 确认问题描述准确。
- [ ] 确认 Flash-first 阈值变更可以接受。
- [ ] 确认 Pro ownership 的表述符合预期。
- [ ] 确认临时 `CODEX_HOME` 的风险说明充分。
- [ ] 确认 Issue 中不包含本机路径、凭据或私人信息。
- [ ] 决定是否需要拆成“Windows/Desktop 修复”和“Flash-first 路由”两个 Issue/PR。

## 九、当前状态

本文档随本次 PR 分支提交到 GitHub，供上游维护者审核；不代表这些修改已被上游合并。
