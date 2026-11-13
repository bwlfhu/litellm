# DeepSeek Messages 交错块修复

2026-11-13，在 22.3 的 `/home/allcam/projects/litellm`、`mainline` 完成修复。基线 `0093f0edc391abb51d5c1be9708a8632f0f979eb`，代码提交 `0a53dbe8f8c16532b0ea62972b9ee35c593924b9`

用户报告 `DeepSeek Messages stream: delta/stop without an open block`。该字符串来自客户端按 index 检查 SSE 生命周期时的拒绝，但故障来源可能是代理或上游。分析阶段已在生产容器确认一个代理缺陷，本轮修复该确定缺陷。没有找到原始故障请求的完整 SSE，不能把构造复现当作该次事故抓包，也不能声称排除所有产生同名错误的上游形态

原实现只保留一个文本块解析器，以全局 offset 重写其余事件。工具块尚未关闭时，另一个文本块被拆为 thinking 和正文，offset 会变化，随后工具 delta 与 stop 被改到已关闭的 thinking index。两个文本块交错时，后一个 start 会覆盖前一个的延迟 start 和解析状态。这两种输入在转换前可由实际 Harness 原生 adapter 接收，经过旧生产兼容层后出现与用户相同的错误

新实现按源 block index 独立保存文本解析器，以及工具和原生 thinking 的固定输出映射。共享计数器只分配新的下游 index，已经发出的 index 不再随其他块的拆分移动。延迟的文本 start 在其他块需要输出时先行发出，保留开始顺序，每个块的标签前缀与尾部仍独立、有界。无效的上游重复 stop 继续交给客户端按原协议拒绝，没有通过吞掉错误或任意补 start 隐藏异常

兼容仅在原有 deployment 开关启用时执行。没有修改模型配置、路由、鉴权、计费、数据库、依赖、静态 UI 或 Chat/Responses 转换代码。本轮仅替换 `inline_thinking.py`，保留上轮 Unicode、截断隔离、连续/嵌套标签及正文示例保护策略

## 验证

新增交错工具、多个活跃文本、反向到达顺序及逐字交错前缀回归，包含 thinking 开关和 1/17/完整字节分块。先在旧代码运行回归，观察到 input_json_delta 被改到已关闭的 index，测试失败，再实施修复。最终 DeepSeek Messages 与 Chat 共 426 项测试通过，类型检查、测试质量门槛和 `make check` 通过，没有生成 schema 漂移

1,000 组随机交错调度在 thinking 开/关下共 2,000 次通过生命周期检查、正文/思考/工具 JSON 内容验证和 Anthropic SDK 累积器验证。随机调度含 2 至 10 个活跃块、原生 thinking、签名、工具、Unicode、未闭标签和非连续源 index。再取 30 组输入及其转换结果，由实际 Harness 原生 DeepSeek adapter 消费，共 60 次成功

22.3 隔离灰度使用与生产相同配置和镜像，额外指向本机合成 SSE 上游。旧生产镜像通过真实 HTTP 接口，在两个有效交错用例、thinking 开/关下共 4 次稳定报出同错。候选镜像在完全相同输入下 4 次全部完成，工具参数 `{"x":1}` 与正文完整。重复 stop 对照在两个 thinking 模式下仍报 MALFORMED_RESPONSE，证明校验没有被绕过。该重放不依赖真实模型随机触发

代理解析微基准中，旧/新普通 10,000 字符逐字 SSE 约 0.275/0.254 秒，64,000 空白约 1.694/1.747 秒，100 万字符 malformed 思考约 1.178/1.230 秒。差异在约 -8% 至 +5% 范围，未观察到明显退化，不代表生产吞吐或 p95/p99。2k/4k/8k/16k 块注册约 0.0055/0.0110/0.0219/0.0439 秒。长思考增量处理缓冲峰值约 2.5 KB。新增映射按源块数占用 O(B) 内存，注册及查找为 O(1)，不缓存整段流

复审重点核对索引分配、多个延迟 start、独立前缀、关闭后的非法 delta/stop、工具 JSON 及签名、客户端提前关闭的既有路径。未发现新的可复现阻断问题。LiteLLM 无 GitNexus 索引，本轮未声称完成图谱、PDG 或 taint 分析

发布镜像为 `registry.allcam.org/litellm/litellm:v1.108.4-open-block.0a53dbe8f8@sha256:7e93fe8c7f563e53fbe29ecfe498b4c2b6b36e38275faeebf656785e4ccde624`。基础固定为原生产 `v1.108.4-thinking-reviewed.0e42eb7bc2@sha256:3dcb78d10130df42fb79ee94deea0a6752b8f8b540945e1460095dbd37fd2e8a`。构建只覆盖 `/app/litellm` 与 Python site-packages 中的解析模块，SHA-256 为 `4962ffac875b368f41ec61fb5e234b454ca7f55c3a7f9db4324773344034525a`

生产发布已完成。候选镜像按 `maxSurge=1`、`maxUnavailable=0` 滚动更新，4/4 Ready、零重启。四个生产副本实际导入解析模块的 SHA-256 均为 `4962ffac875b368f41ec61fb5e234b454ca7f55c3a7f9db4324773344034525a`，每个副本独立重放交错事件均通过，非法重复 stop 仍被拒绝。Chat、Responses、官方 Messages 及真实第三方工具续聊灰度验收通过。正常 autocode Harness 完成 4 次工具调用和 4 次结果回传，最终为 `OPEN_BLOCK=OK`、`STEPS=4`，stderr 为空。灰度、诊断密钥、构建 Job 和端口转发已清理，构建上下文已归档后删除。本次没有新建分支或 worktree

发布后仍未捕获原始偶发线上 SSE，因此若再次出现同名错误，需要保留请求时间、模型和 request ID 以便抓取原始事件序列

回退使用上述基础镜像，会恢复本次已确认的交错块缺陷。后续若再出现同名错误，应结合模型、时间或请求 ID 判断是否为其他上游无效事件，不能仅由该错误文本判定本次修复无效
