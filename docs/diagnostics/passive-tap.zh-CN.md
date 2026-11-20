# LiteLLM 旁路失败诊断工具

本文说明宿主机上的被动 HTTP 诊断工具。该工具独立于 LiteLLM 部署，不是 LiteLLM 内置功能，源码和运维脚本位于部署仓库的 `diagnostics/passive-tap`，安装后运行于 `/opt/litellm-passive-tap`，由 `litellm-passive-tap.service` 管理。

## 设计目标

工具用于在短时间窗口内保留失败 API 调用的客户端请求和可见错误，辅助调查上下文长度、生成参数、SSE 错误及传输异常。它以旁路方式监听 LiteLLM Pod 的宿主机虚拟网卡，不代理、不转发、不应答业务流量，不改 LiteLLM 代码、配置、Service 或 Ingress。

HTTP 请求正文只在内存中暂存。成功请求完成后立即释放，只有匹配触发规则的失败样本才进入限额内存池。默认状态为关闭，开启必须指定到期时间，服务重启不会恢复采集状态或样本。

## 架构与诊断边界

采集器通过 libpcap 读取 Pod veth 上的 TCP 4000 报文副本，并使用 h11 重组和解析 HTTP/1.1。它每 30 秒发现一次 LiteLLM 容器及对应 veth，因此可随 Pod 更新重新接入。控制接口是 root 专用 Unix socket，没有公网管理端口。

工具看到的是客户端到 LiteLLM 的明文请求和 LiteLLM 返回给客户端的响应。它看不到 LiteLLM 到模型供应商的 HTTPS 明文，也看不到 LiteLLM 内部参数转换、供应商重试或被 fallback 恢复的错误。因此客户端请求与返回错误能证明外部表现，但不能单独证明上游实际收到的参数。

上游平台的失败记录和 LiteLLM Router 的单次尝试异常，不一定对应客户端最终失败。内部重试或 fallback 恢复后，成功请求会按设计被释放，上游 request ID 也未必出现在客户端响应中。调查时先确认错误来源，再用 request ID 匹配实际失败样本。不能把未匹配直接解释为抓包丢失。

开启前已经建立的 TCP 连接，其当前事务会被跳过；遇到下一条完整可识别的 HTTP/1 请求行后，采集器会在原连接上同步并配对响应。若采集从请求中段开始，该事务不会被猜测重组。HTTP/2、TLS、IP 分片、抓包丢失和不支持的压缩编码会降低覆盖率。超大 SSE 事件和提前断开的响应可能无法完成语义解析。`status` 中的丢包、重组、编码和解析计数应与每个样本的完整性标记一起判断。

## 触发规则

仅检查生成 API 路径，包括 Chat Completions、Anthropic Messages、Responses 和 Completions。健康检查和其他非生成 API 不保留正文。

失败触发类型如下：

| 类型 | 判定 |
| --- | --- |
| `http_error` | 最终 HTTP 状态码为 400–599，默认全部捕获，可按状态码过滤 |
| `sse_error` | SSE `event: error`、JSON 错误字段，或 `error`、`response.failed`、`response.incomplete` 事件类型，包括 HTTP 200 响应 |
| `json_error` | HTTP 2xx JSON 响应顶层包含非空 `error` |
| 传输异常 | TCP reset、连接结束前响应不完整、TCP 缺口、HTTP 解析错误或诊断超时；样本表示证据可疑或不完整，不等同于供应商故障 |

可按 model 名子串过滤。多个 model 子串为 OR；model 过滤会与失败触发共同生效。请求正文无法完整解析时，model 是未知，指定 model 的过滤规则不会将未知请求保存为正文样本。

## 开关与常用操作

```bash
# 查看模式、剩余时间、接口、内存用量、丢包和触发计数
sudo litellm-tap status

# 查看失败样本目录，不显示正文
sudo litellm-tap list

# 开启 DeepSeek 采集一小时，单请求最多暂存 32 MiB，到期自动关闭
sudo litellm-tap arm --mode capture --seconds 3600 --models deepseek --request-mib 32

# 捕获任意模型 15 分钟，使用默认 8 MiB 请求上限
sudo litellm-tap arm --mode capture --seconds 900

# 只统计失败元数据，不把正文保存为失败样本
sudo litellm-tap arm --mode metadata --seconds 300

# 只匹配指定 HTTP 状态码。SSE 和 JSON 错误规则仍会生效
sudo litellm-tap arm --seconds 900 --models deepseek --codes 400,429,500-599

# 聚焦 HTTP/SSE/JSON API 错误，不保存连接中断或解析异常样本
sudo litellm-tap arm --seconds 3600 --request-mib 32 --no-transport-errors

# 查看不含正文的结构分析
sudo litellm-tap analyze CASE_ID

# 显式查看请求正文、响应前缀和错误事件。输出可能含用户数据
sudo litellm-tap show CASE_ID

# 立即关闭抓包。已捕获样本按 TTL 保留
sudo litellm-tap off

# 删除一条或全部失败样本
sudo litellm-tap clear CASE_ID
sudo litellm-tap clear

# 停止服务并取消自启动
sudo systemctl disable --now litellm-passive-tap
```

`arm` 持续时间范围为 1 秒至 4 小时。重新 arm 会结束旧窗口并丢弃尚未完成的请求。`off` 关闭采集但不清除已保存样本；`clear` 删除内存样本；`disable --now` 停止控制服务并清空进程内状态。服务再次启动后仍为 OFF。

指定 `--models` 会在请求正文解析出 model 后应用过滤，model 未知的错误也会被过滤。调查高上下文错误但尚未确认请求中的 model 值时，不传 `--models`，避免排除目标样本。

`metadata` 模式仍会暂存请求数据，以便解析 model 和错误类型，但不会把正文保存在失败样本池中。关闭后或请求结束后，暂存数据会释放。

## 触发后的分析

先用 `list` 取得样本 ID，再运行 `analyze CASE_ID`。摘要会给出 model、输入项数量、字符串字符数、`max_tokens`、`max_completion_tokens`、`max_output_tokens`、thinking budget 和常见错误分类。字符数不是 tokenizer 结果，不能据此断定供应商 token 超限。

`budget_gte_max_tokens=true` 表示客户端输入中的 thinking budget 不小于输出 token 上限，是待调查线索，不证明 LiteLLM 转换后的上游参数相同。Responses 输入会额外统计 reasoning、工具调用等 item 类型，以及 reasoning item 是否带有文本字段，便于排查历史回传问题。

如需核对原始失败输入，使用 `show CASE_ID`。请求正文可能包含用户提示、代码、工具参数或工具结果。默认不输出正文的 `status`、`list` 和 `analyze` 更适合日常检查。导出到普通文件会产生磁盘写入；确有需要时应使用 `/run` 等 tmpfs、限制权限并及时删除。不要把需要跨服务重启保留的导出放在 `/run/litellm-passive-tap`，systemd 会在服务重启时清理这个运行目录。

请求头采用白名单，Authorization、API key、Cookie 和 URL query 不会保存。请求正文仍可能带敏感信息，因此只有 root 可访问控制 socket 和样本。工具不向应用日志或 stdout 写入请求正文。

## 资源上限与保护

| 项目 | 限制 |
| --- | --- |
| 单请求正文 | 默认 8 MiB，可调至最高 32 MiB；超出时标记截断 |
| 响应正文 | 最多保留 128 KiB 前缀 |
| 单 SSE 事件 | 最多解析 64 KiB |
| 暂存请求正文 | 384 MiB |
| 失败样本正文 | 128 MiB，最多 100 条 |
| 失败样本保留时间 | 60 分钟 |
| 保存速率 | 每分钟最多 30 条；每 model 和触发类型最多 10 条 |
| TCP 重排缓存 | 每方向最多 256 KiB、128 段；缺口超过 5 秒放弃该连接 |
| 活跃连接 | 最多 256 条，每条连接最多 8 个待配对请求 |
| 抓包环形缓冲 | 每个接口 8 MiB |
| systemd 资源限制 | MemoryHigh 768 MiB、MemoryMax 1 GiB、禁用 swap、CPUQuota 50% |

资源达到上限时优先丢弃诊断副本或旧样本，不阻塞 LiteLLM 请求转发。`--no-transport-errors` 可避免将 HTTP 200 后的断流、解析错误等可疑记录保存为失败样本，适合集中捕获模型 API 的 HTTP/SSE/JSON 错误。运行期间不创建 pcap 文件，不持久化成功请求，也不写数据库。服务安装和 systemd 生命周期记录仍会产生少量系统级磁盘写入。

## 状态检查与维护

```bash
sudo systemctl show litellm-passive-tap \
  -p MainPID -p MemoryCurrent -p MemoryPeak -p CPUUsageNSec \
  -p IOReadBytes -p IOWriteBytes -p NRestarts
sudo cat /sys/fs/cgroup/system.slice/litellm-passive-tap.service/io.stat
sudo cat /sys/fs/cgroup/system.slice/litellm-passive-tap.service/memory.events
```

`pcap_dropped` 或 `tcp_gap` 增长表示采集覆盖不完整。`midstream_synchronizations` 表示在既有连接上同步的次数。`pending_budget_exhausted` 表示待处理正文预算不足，会影响请求完整性。`sse_event_oversize`、`http_parse_error`、`unsupported_response_encoding` 和响应提前结束计数也应作为样本完整性限制处理。计数跨 arm 累积，判断新窗口时应比较前后增量。判断磁盘 IO 时看进程 `read_bytes`/`write_bytes` 或 cgroup `io.stat`，`/proc/PID/io` 中的 `rchar`/`wchar` 也会统计 socket 通信。

部署、升级和隔离网络测试脚本位于运维目录 `diagnostics/passive-tap`。升级时先运行 `off`，再安装版本并重启诊断服务。离线解析测试命令为 `python3 -B -m unittest -v test_core.py`；libpcap 隔离测试命令为 `sudo python3 -B integration_test.py`。隔离测试不访问真实模型。
