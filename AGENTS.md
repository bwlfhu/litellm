# LiteLLM 维护约定

上游仓库原有要求：进行代码修改前先阅读仓库根目录的 `CLAUDE.md`，并遵守其中的编码与测试规范。

## 目标

维护 LiteLLM 的最小、可回滚补丁，优先解决 Responses API 流式响应的兼容性问题；不得把线上环境配置、模型路由或密钥提交到本仓库。

## 上游与版本

- 上游仓库：`https://github.com/BerriAI/litellm.git`
- `upstream` 指向官方仓库；`origin` 指向我们的 GitHub fork。
- 修复分支以官方 release tag 为基线，例如 `custom/v1.94-responses-fix` 基于 `v1.94.0`。
- 不直接跟踪官方开发分支作为生产基线；升级时先同步新的 release tag。

## 当前修复范围

优先验证并移植官方相关修复：

- LiteLLM #34754 / PR #34759：Responses 流式终态响应为 `dict` 时，安全读取 `usage`，覆盖 Router 和 logging 路径。
- PR #35018：Responses 流式 bridge 正确初始化 iterator 状态并保留中途错误/fallback 能力。
- LiteLLM #35197 / PR #35200：Responses 速率限制器状态只写入 `litellm_metadata`，避免内部字段泄漏为 provider 可见的 `metadata`。
- 相关官方链接：
  - `https://github.com/BerriAI/litellm/issues/34754`
  - `https://github.com/BerriAI/litellm/pull/34759`
  - `https://github.com/BerriAI/litellm/pull/35018`
  - `https://github.com/BerriAI/litellm/issues/35197`
  - `https://github.com/BerriAI/litellm/pull/35200`

移植前必须检查 PR 是否依赖比当前基线更新的代码；不要无条件合并整个 PR 分支。优先提取最小源码改动，并保留回归测试。

## 提交与同步

- 每个逻辑修复单独一个 commit，例如：
  - `fix(responses): handle dict-shaped terminal responses`
  - `fix(responses): preserve original stream errors during fallback`
  - `test(responses): cover dict response and mid-stream fallback`
- 官方发布新版本后：
  1. `git fetch upstream --tags`
  2. 将自定义分支 rebase 或 merge 到新的官方 tag
  3. 解决冲突并运行测试
  4. 若官方已包含相同修复，删除重复的自定义 commit
- 使用 `git range-diff` 检查自定义补丁相对官方版本的剩余差异。
- 不在运行中的 Pod 内编辑源码；始终构建带有明确版本和 patch 标识的镜像。

## 验证要求

至少覆盖：

- `/v1/responses`、`stream=true` 正常响应；
- `response.failed` 在首个 token 前和中途出现；
- `response.completed` 的 `response` 为 Pydantic 对象和普通 `dict`；
- fallback 执行、fallback 失败以及原始错误保留；
- SpendLog、usage 和 cost 正常写入；
- 不再出现 `dict object has no attribute usage`。

未通过上述验证前，不得用于线上 LiteLLM 镜像。

## 安全边界

- 不提交 API key、数据库密码、Kubernetes Secret、线上请求正文或敏感 headers。
- 模型 URL、Team 白名单和线上部署配置属于外部运行环境，不放入本仓库的源码修复提交。
