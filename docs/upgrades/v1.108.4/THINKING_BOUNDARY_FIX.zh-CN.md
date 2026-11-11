# DeepSeek thinking 边界修复记录

本记录保留上一轮修复的历史证据。后续审查发现的截断、Unicode、连续标签、性能和 Dashboard 类型问题已另行修复，当前行为与生产值见 [thinking 审查修复记录](THINKING_REVIEW_FIX.zh-CN.md)

2026-11-11，LiteLLM v1.108.4 定制版。

第三方原生 Anthropic Messages 路由偶尔把思考放在 text 中。首次兼容修复支持规范的 `<thinking>…</thinking>` 和 `<think>…</think>`。后续线上会话出现 `<thinkingGood.…</thinking>`，开标签缺少 `>`，严格开标签匹配未命中，思考继续作为正文转发。

异常会话第 34 步保存的首个 text 片段为 `<thinkingGood`，text 共 2,455 字符，原生 reasoning 只有一个空格。相同响应 ID 的路由记录确认请求命中第三方原生 Messages endpoint，没有重试或 fallback。历史上游原始响应没有留存。重建前 33 步上下文后直连真实上游返回正常工具调用，因此该次重建不作为历史抓包或随机故障重现的依据。

代码提交 `7e6da86645ab69c2f06ed9e9d9258da7da240358` 已进入 mainline。兼容解析器先暂存不规范的开头，遇到相应闭标签才把其中内容转为 thinking，闭标签后的正文正常发送。没有闭标签的相似普通文本保留原样。顺序缓冲避免逐字输入反复扫描长内容。可疑前缀的分类可能等待闭标签或块结束；该行为防止内容先作为正文泄漏。

本次保持既有的 deployment 兼容开关范围。官方路由和其他未启用部署不会进入该解析器。没有更改工具 JSON、模型参数、依赖、UI 或数据库 schema。

基础镜像是当时线上 `v1.108.4-inline-thinking.86a96a07f9`，digest `sha256:c19be47258da691a6ce551ab930a05b18e5b51f9d280a3892006c733c4f474f0`。最新构建仅覆盖 `inline_thinking.py` 在 `/app/litellm` 和 Python site-packages 中的两份文件。

发布镜像为 `v1.108.4-thinking-boundary.7e6da86645`，digest `sha256:8f0644a5a655fbaacdccd1e350655cc96b79b4b4abf24f18b1ce71d621b3ebc1`。四个生产副本均加载相同模块 SHA-256 `a615759d452f819a4545ef71011febe1e4b2d38928faf4d2dcb18513fab47d91`。

事故原文在旧代理普通/流式响应中都作为 2,464 字符正文出现，包括诊断答案。新灰度代理正文均为 9 字符 `RESULT=OK`，去标签后 2,435 字符思考的 SHA-256 与事故原文一致。Mac 和服务器真实 Harness 运行时均完成相同事故重放，只有 thinking 事件包含思考，最终正文为 9 字符，stderr 为空。

真实第三方模型普通/流式工具调用及结果续聊成功。相关 394 项自动测试及 `make check` 通过，覆盖逐字/字节分块、中文、长思考、未匹配普通文本、工具输入和后续块索引。生产采用四副本滚动更新，发布后 4/4 Ready、零重启。

发布后通过正常 autocode 入口运行 Mac Harness，完成四次独立工具调用和四次工具结果回传，最终仅一个 text 事件，输出 `PRODUCTION=OK` 和 `STEPS=4`，stderr 为空。诊断密钥、隔离灰度、构建 Job 和测试隧道已清理。

回退使用前述基础镜像。回退会恢复旧解析器，不能继续修复本次不规范标签。旧会话已经保存的泄漏正文不会由本次代码发布自动改写。
