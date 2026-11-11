# DeepSeek thinking 审查修复记录

2026-11-11，在 22.3 的 `/home/allcam/projects/litellm`、`mainline` 上完成修复。审查基线为 `7004579e5800a149f5227b87e3e0bd9ed9f0aa03`，代码提交为 `0e42eb7bc20fb2f811bb3089382b3d07d231cd7b`

本记录取代上一轮边界修复中“等待闭标签，否则保留正文”的分类策略。第三方返回 `<thinkingGood...` 并以 `max_tokens` 或 `end_turn` 截断时，已识别的思考仍作为 thinking 处理，不会在结束时降级成正文。关闭 thinking 时继续通过原有 sanitizer 隐藏这部分思考，保留 stop_reason

流式和非流式共用一个增量解析器。标签匹配在原字符串上进行，只对 ASCII 标签忽略大小写，避免 Unicode `İ` 小写展开后造成字符坐标错误。解析器支持正文开始前的连续和嵌套包装，长短闭标签按相同深度规则处理。正文开始后不再解析内嵌标签，答案中的示例和工具 JSON 保持原样

标签前探测最多保留 64 个字符，思考中最多保留 10 个字符的闭标签尾部。空白立即向下游输出，不反复扫描历史空白，也不再缓存整段思考。SSE normalizer 与既有 sanitizer 共用已解析的 JSON，减少重复编解码。非文本 delta 在转发前确保对应 block 已开始，补充了首个 delta 为 citation 的生命周期回归

兼容层不能可靠推断任意文本的语义。本部署将开头的 `<thinking`、`<think` 作为保留前缀。短探测范围内完整的 `<thinkingExample>`、`<thinkpad>` 等普通扩展标签仍按正文保留。超过探测长度或不完整且歧义的保留前缀优先作为思考隔离。若需要原样输出这类字面字符串，应先加普通说明或代码围栏。这项策略只对明确启用兼容的 deployment 生效，不是通用 XML 解析器

Dashboard 的 ObjectPermission 类型改为引用领域模型 `litellm__models__object_permission__LiteLLM_ObjectPermissionTable`，没有手改生成 schema。API 类型同步检查追加生产 tsconfig 的类型检查，防止再次出现生成类型更新而消费者编译失败。该改动只有 TypeScript 类型，没有改变 UI 运行逻辑

修复后逐项复审了前次 6 项问题，以及 citation 索引和 SSE 重复编解码。没有发现新的可复现阻断问题。复审以完整源码 diff、调用者检查和可执行探针为依据。没有 LiteLLM GitNexus 索引，因此没有声称完成图谱、PDG 或 taint 验证

407 项 DeepSeek Messages 和 Chat 回归测试通过，覆盖 thinking 开关、max_tokens/end_turn、所有均匀 delta 分段、Unicode、连续/嵌套包装、普通示例和工具索引。另有 1,000 组随机文本及字节分块通过流式/非流式一致性和 Anthropic SDK 累积器验证，200 组多块与工具生命周期检查通过。Dashboard 生产类型检查、6 项权限组件测试和 `make check` 通过，生成文件没有额外漂移

22.3 上的解析微基准中，64,000 个逐字空白 SSE delta 处理约 1.89 秒，上一轮代码约 6.63 秒。只测解析器时，8k/16k/32k/64k 空白分别约 0.016/0.032/0.065/0.126 秒，符合线性增长。处理 100 万字符 malformed 思考的额外解析缓冲峰值约 2.5 KB，上一轮约 3.25 MB。普通 6,000 字符逐字 SSE 在兼容关闭/开启时约 0.142/0.188 秒，开启仍有约 33% 的代理解析开销，低于上一轮约 70%。这些数字不代表模型总耗时、线上吞吐或 p95/p99，没有做线上并发压力测试

隔离灰度先使用旧生产镜像，通过真实 HTTP 证实截断、Unicode、连续包装和混合闭标签问题。替换候选镜像后，8 种输入的普通与流式共 16 项验收通过。原事故正文重放的思考 SHA-256 仍为 `19d93e45ccecd8fb508cffe9e6a573c04e657a73d0c599c34620b09de800100d`，最终正文只有 `RESULT=OK`。Mac 和 21.139 的 allcam Harness 运行同一事故重放均成功，各产生 2 次 thinking、1 次 text，最终正文 9 字符，stderr 为空

真实第三方原生 Messages 普通/流式工具调用和工具结果续聊通过，Chat 和官方 Messages 验收通过。Responses 新旧版本各 3 次同请求对照通过，另有显式 reasoning 参数的对照通过。第一次扩展探针未区分 message 和 reasoning 项，将所有 output.content 拼为正文，不能用该结果判断正文泄漏。那次完整响应没有保留，因此不推断其各项归属。修正探针只读取 message 后，Chat、Responses、官方 Messages 再次全部通过。Responses 走既有 Chat 转换路径，本次没有更改该路径

发布镜像为 `registry.allcam.org/litellm/litellm:v1.108.4-thinking-reviewed.0e42eb7bc2@sha256:3dcb78d10130df42fb79ee94deea0a6752b8f8b540945e1460095dbd37fd2e8a`。基础为当时生产的 `v1.108.4-thinking-boundary.7e6da86645@sha256:8f0644a5a655fbaacdccd1e350655cc96b79b4b4abf24f18b1ce71d621b3ebc1`，只覆盖 `inline_thinking.py` 和 `transformation.py` 在 `/app/litellm` 与 site-packages 的两份文件。原有依赖、静态 UI 和定制功能均继承基础镜像，没有数据库迁移或模型参数改动。Dashboard 的类型修复用于今后的完整构建，本次没有重新构建静态 UI

解析器 SHA-256 为 `738ffe3c7af2a0a9b93db413a353477664cda738f335c06336db36dd73bbe62c`，转换模块为 `e03d9551a292e5749cc7719179ddcd669fe9c5d346a292dedf3d0687d3d7aba3`

生产按 `maxSurge=1`、`maxUnavailable=0` 滚动更新完成，4/4 Ready、零重启。逐个副本实际导入模块与源码哈希一致，源码目录和 site-packages 的两份文件也都一致，每个副本独立真实 Messages 请求均返回 `POD=OK`。对发布前后 Pod 配置作结构比较，除镜像外全部相同，模板原有 annotation 保留，只增加本次代码 revision 标注。数据库只读检查确认兼容开关仍只在原有问题 deployment 启用

发布后通过正常 autocode 入口运行 Mac Harness，完成 4 次独立 bash 工具调用和 4 次结果回传，最终正文为 `REVIEWED=OK` 与 `STEPS=4`，stderr 为空，没有 thinking 标签进入正文。灰度资源、诊断密钥、构建 Job 和隧道已清理，构建上下文归档到受限备份目录。本次没有新建 Git 分支或 worktree，仓库只保留原 mainline 工作区

回退使用上述基础镜像。回退将恢复旧解析器及已知缺陷。旧会话已经保存的正文不会被发布自动改写
