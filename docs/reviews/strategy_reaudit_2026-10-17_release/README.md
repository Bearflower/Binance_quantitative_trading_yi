# 发布前复核（2026-10-17）

本次授权：用户要求再次复核，无阻塞问题后push并上线部署。历史审查材料保持冻结。

## 复核结果

- 单毫秒闭区间首次查询已修复：独立案例[1000,1000]实际请求1次、返回1条；income超页重跑仍保持incomplete。
- 定向功能测试178项通过（性能项单列）。扩大验证274项通过、2项因new_coin测试夹具缺min_notional失败；按真实构造函数默认值补齐夹具后，该文件21项全部通过。原扩大集19项成功与21项重跑有重叠，合计454个不同功能测试通过。
- 性能项仍未达5000阈值。同环境抽取HEAD评分代码、使用相同配置及同一测试循环也未达标（约3849/秒）。支持“失败不是本次守卫修复新增”，不据此证明具体平台倍数，也不修改阈值。
- 文档已经明确income完整采集和净收益消费端门禁未完成。相关文档表述不再作为生产自动拦截证据。
- 生产部署前只读检查：PostgreSQL15.19；本次新增证据表尚不存在，HRS归属两列尚不存在。部署后需要验证迁移和真实写入。
- 本次唯一附加修改：tests/test_strategies/test_fill_fact_consistency.py的最小executor夹具补齐min_notional默认值，未改变生产交易逻辑。

## 发布边界

仅发布本轮策略修复、证据采集模块、相关测试和文档。未纳入工作区行情JSON、临时覆盖率文件、无关审计脚本及任务笔记。原有main上的文档提交将随fast-forward一并推送。

income同毫秒完整补采与净收益消费端门禁仍是能力限制。线上数据库迁移及信号归属列写入在部署后独立验收，不以本地mock代替。

## 验证材料

- functional_tests.txt：178项功能测试。
- extended_tests_before_fixture.txt：274通过，2项夹具缺字段失败。
- fill_tests_after_fixture.txt：补齐夹具后21通过。
- tests_with_performance.txt、baseline_performance.txt：当前与HEAD基线性能测试实际输出。
- verify_point_window.py、remaining_results.json：更新正确预期的独立验证；历史脚本不改动。

部署结果待本次实际发布后记录。
