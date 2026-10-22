# 1.106.2 计费、预算、路由独立复核

复核时间：2026-10-22；初始审查源码 `fb1a245e45`，最终复核到 `9991444d4d`。基线为 `abadbbd2cc`，官方目标为 `49943cd52e`（v1.106.2）。本复核只读源码、测试及证据，没有修改源码、生产资源或业务价格

## 结论

未发现尚未解决的计费、预算、路由合并阻断项。初查发现 3 项价格目录测试失败，主会话已修复；我独立确认修复后两份价格目录没有重复键，且 `json.loads(9991444d4d) == json.loads(fb1a245e45)`，因此修复没有改变之前实际生效的任何目录值

代码准入与线上验证需分别记录。本复核没有替代候选镜像及生产环境的真实请求、费用持久化和临时 key 清理验收，也没有把旧源码上的 make check 结果标为最终源码重新运行通过

## 逐项核对

| 项目 | 结果及依据 |
| --- | --- |
| 官方计费修复 | `cost_calculator.py`、`llm_cost_calc/utils.py`、batch 校验/工具、`proxy_track_cost_callback.py`、`db_spend_update_writer.py`、`proxy/utils.py`、`budget_limiter.py`、`model_max_budget_limiter.py` 与 v1.106.2 对应文件一致，未因保留定制而遗漏官方改动 |
| 部署价格隔离 | `router.py` 保留部署独立注册、按部署 ID 查询、刷新重放、缓存/阶梯/服务等级价格继承；同时合入官方 `deployment_pricing_models` 和 Bedrock profile 价格查找。`utils.py` 合入显式费率覆盖的跟踪及继承价格过滤，未发现将部署价格重新写回共享后台模型的回归 |
| 标准费用日志 | `litellm_logging.py` 保留定制的真实 deployment/base_model 回退、顶层 model_info、dict/模型 Responses usage 转换、上游返回 cost 的保留；官方 PTU spillover、Vertex location、router ID 价格信息和 batch 定价入口均存在。PTU spillover 下明确跳过部署零价回退 |
| Responses 中断计费 | 官方 `partial_responses_incomplete_event_on_disconnect` 已接入共用请求处理器；Chat 转 Responses 迭代器能从已收集 chunk 组装 `response.incomplete`；路由包装器保留 billing source 并在切换后合并 primary/fallback usage。已有最终成功回调标志仍先于中断计费检查，以避免重复结算 |
| 异步成功日志去重 | `Logging` 保留 claim 机制并加入官方 `async_success_log_enqueued` 状态，`utils._schedule_async_success_logging` 在实际排入任务时标记，支持 deferred logging 与失败计费区分 |
| 预算 CAS/Lua | `reset_budget_job.py`、`spend_counter_reseed.py`、`verification_token_repository.py`、Redis generation/counter 定制相对生产基线没有改动；官方预算限制文件原样合入，未覆盖本机窗口重置并发保护 |
| 路由及观测定制 | 路由可信 deployment context、callback 的服务端 model_info、跨实例 routing metadata、阻断部署过滤、冷却配置优先级保留；同时合入官方 service-tier timeout、retry attempt model/base_model 及新的价格模型查找 |
| 数据库差异 | 对所有受跟踪 `schema.prisma` 及 `/migrations/` 路径逐项比较 `abadbbd2cc..9991444d4d`，差异为空，包含主 schema、proxy schema、proxy extras schema 及 SQL 目录。因此“无需新增 schema 迁移”准确，不代表应用的普通费用写入行为没有变化 |
| 可选向量元数据清理 | 官方新增 `db_scripts/strip_vector_store_internal_metadata.py` 是独立、默认 dry-run 的上游元数据整理脚本，不是 Prisma 启动迁移。本复核未运行该脚本，不应把它列为数据库迁移完成 |

## 测试证据

`regression-complete.xml` 和 `.log` 相互一致：15,330 项中 15,325 passed、3 failed、2 skipped，3 项失败全部来自新价格 schema/重复键校验，不能将该次运行宣称为全部通过。未见计费、预算、路由或 Responses 用例失败。该集合中包含 2,113 个 router、1,513 个 cost、1,174 个 responses、293 个 spend、182 个 budget 类用例，分类可能重叠，不能相加

三个失败分别为 `test_committed_schema_matches_generator_output` 和主/备份价格目录的 `test_price_map_has_no_duplicate_keys`。我对官方、原生产基线及初次合并结果独立读取，确认重复键在 `abadbbd2cc` 已存在，官方 v1.106.2 本身无重复。重复键为 `scaleway/deepseek-v4-flash-0731`、`deepseek-flash`、`deepseek/deepseek-flash`、`deepseek-v4-flash-vision-exp`、`deepseek/deepseek-v4-flash-vision-exp`。部分重复项有额外字段，不能任意保留第一项

`9991444d4d` 删除先出现项并同步 schema；我独立验证主/备份目录全部实际生效值不变、重复键数为零。`pricing-schema-fix.log` 记录受影响测试 105 passed。首次旧虚拟环境的 batch 测试失败已被当前环境的完整运行覆盖，不作为产品回归

`make-check-final.log` 完整结尾为 `check: PASS`，包括 Python lint、test-tree lint、dashboard lint 和 API 类型同步，basedpyright 为 0 errors / 0 warnings / 0 notes。此日志先于最后的价格目录修复提交，最终发布证据需另行记录该小幅修复之后实际执行的检查范围

## 验证边界

本次完整 XML 不包含 `tests/integration/spend/test_local_budget_generation.py` 或 `test_local_budget_reset.py` 的真实 PostgreSQL/Redis 集成结果，这些实现文件本次没有变更。当前报告仅确认其未被合并覆盖；不冒用上次升级的集成测试结果作为本次新运行证据。生产切换前后仍应核对真实请求的非零费用、持久化行和路由归属，尤其保留一次 Responses 及 Messages 工具历史请求
