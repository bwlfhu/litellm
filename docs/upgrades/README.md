# LiteLLM 升级运行指南

本指南只保留影响线上安全和可靠性的检查项。原始请求、数据库 dump、凭据、构建缓存和临时 worktree 不进入仓库，发布完成后清理

## 升级前

确认当前默认分支和工作区干净，拉取官方 tag，比较当前版本、目标版本和本地定制改动。只迁移仍然需要的定制行为，并检查官方是否已经覆盖旧补丁

核对依赖和数据库变更：

```bash
uv lock --check
python -m compileall -q litellm enterprise
git diff --stat <current>...<target>
git diff <current>...<target> -- litellm/proxy/schema.prisma litellm/proxy/migrations
```

运行 `make check` 和受影响协议、提供方、计费及路由测试。失败项必须修复或明确排除，不能只记录为历史取证

## 候选镜像

使用固定版本的构建器和依赖源构建候选镜像，记录源码提交、镜像 digest 和容器内版本。候选环境使用隔离数据库、缓存和密钥

至少验证：容器启动、Rust 扩展导入、数据库连接、readiness、Chat Completions、Anthropic Messages、Responses、工具调用和费用记录。验证请求使用专用测试 key，完成后删除测试资源

## 生产发布

发布前确认 schema migration 数量、checksum 和是否需要执行迁移。没有迁移时保持 `DISABLE_SCHEMA_UPDATE=true`，不要为了“验证”执行无关的数据清理

采用 `maxSurge=1`、`maxUnavailable=0` 的滚动发布。发布后逐 Pod 检查镜像 digest、版本、Ready 状态和重启次数，并检查 readiness 的数据库状态、Service、Ingress、关键 API 和 `LiteLLM_SpendLogs` 成功写入

出现异常时优先停止继续发布并保留当前业务数据。只有在 schema 兼容、问题范围明确且上一镜像可用时才回退镜像，不直接用旧数据库备份覆盖新写入

## 收尾

只保留脱敏的发布记录：目标版本、源码提交、镜像 digest、迁移结论、测试结果、Pod 健康状态和已知限制。删除数据库 dump、kubeconfig、凭据、原始请求响应、私有日志、构建缓存、临时 worktree 和临时分支

```bash
git status --short --branch
git worktree list
git branch --all
```

最终状态应为主分支与远端一致、工作区干净、没有临时 worktree 或临时分支
