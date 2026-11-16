# W0 可行性验证报告

- 模型：`deepseek:deepseek-chat`
- 样本：15 次
- 时间：2026-11-15 00:13

## 汇总

| 指标 | 结果 |
|---|---|
| 路径命中率 | 100.0% |
| 参数命中率 | 80.0% |
| 终局命中率 | 93.3% |
| 过度追问 | 1 次 |
| 编造设备号 | 3 次 |
| 语义正确率 | 73.3% |
| 护栏拦截 | 12 次 |
| 修正重试 | 4 次 |
| fc 解析失败 | 0 / 81 |
| 平均步数 | 1.5 |

**结论：勉强可用，必须保留参数校验与证据溯源，或换模型对比**

## 明细

| 轨迹 | 轮 | 实际调用 | 路径 | 参数 | 终局 | 溯源 | 编造 | fc失败 | 回复摘要 |
|---|---|---|---|---|---|---|---|---|---|
| H1 | 1 | query_device_profile → query_defect_log → query_defect_log → query_defect_log → query_light_usage | ✓ | ✗ | ✓ | ✓ | - | 0 | ## 结论  **主因指向光源衰减，不是模型或阈值问题。** CX-101 的光源累计时长已超额定寿命，亮度设定被下调过 |
| H1 | 2 | query_device_profile → query_defect_log → query_defect_log → query_defect_log → query_light_usage | ✓ | ✗ | ✓ | ✓ | - | 0 | ## 结论  CX-101 的误判**主因指向光源老化衰减**，不是批次问题。夜班更严重，与夜间环境光更弱、光源衰减影响 |
| H1 | 3 | query_device_profile → query_defect_log → query_defect_log → query_defect_log → query_light_usage | ✓ | ✗ | ✓ | ✓ | - | 0 | ## 结论  CX-101 的误判**方向指向 1号工位的光源衰减**，夜班更严重与光源输出不足叠加夜班环境光有关。但现 |
| A1 | 1 | query_device_profile → query_defect_log → query_defect_log → query_defect_log → query_defect_log → query_light_usage | ✓ | ✓ | ✓ | ✓ | - | 0 | ## 结论  **大概率是料的问题，但更准确地说：是“新供应商这批料的表面特性与现有检测阈值/模型不匹配”，而不是设备硬 |
| A1 | 2 | query_device_profile → query_defect_log → query_defect_log → query_defect_log → query_light_usage → query_defect_log | ✓ | ✓ | ✓ | ✓ | - | 0 | ## 结论  **大概率是料的问题，但更准确地说：是模型/阈值没有适配新供应商的物料，而不是设备硬件故障。** 建议先按 |
| A1 | 3 | query_device_profile → query_defect_log → query_defect_log → query_defect_log → query_light_usage → query_defect_log | ✓ | ✓ | ✓ | ✓ | - | 0 | ## 结论  **大概率是料的问题，但更准确地说：是“新批次物料 + 现有模型/阈值未适配”共同导致的，不是设备硬件故障 |
| A5 | 1 | query_device_profile → query_defect_log → query_defect_log → query_defect_log → query_defect_log → query_light_usage | ✓ | ✓ | ✓ | ✓ | - | 0 | ## 结论  划痕误判是**模型/阈值层面的问题**，不是硬件故障。光源、镜头、标定都正常，不需要拆机维护；建议调整划痕 |
| A5 | 2 | query_device_profile → query_defect_log → query_defect_log → query_light_usage → query_defect_log → query_defect_log | ✓ | ✓ | ✓ | ✓ | - | 0 | ## 结论  问题集中在**划痕这一类缺陷的判定阈值/模型**上，不是硬件故障。划痕误判率 60.9%（23 条里 14 |
| A5 | 3 | query_device_profile → query_defect_log → query_defect_log → query_light_usage → query_defect_log → query_defect_log | ✓ | ✓ | ✓ | ✓ | - | 0 | ## 结论  问题集中在**划痕这一类缺陷的判定阈值/模型**上，不是光源衰减或镜头污染导致的硬件问题。建议优先调整划痕 |
| I1 | 1 | query_device_profile → query_defect_log → query_defect_log → query_defect_log → query_defect_log | ✓ | ✓ | ✓ | ✓ | CX-101 | 0 | 需要先确认设备信息：请问是哪一台？请提供设备编号。不确认清楚我不做诊断，避免猜错设备。 |
| I1 | 2 | query_device_profile → query_defect_log | ✓ | ✓ | ✓ | ✓ | CX-101 | 0 | 需要先确认设备信息：请问是哪一台？请提供设备编号。不确认清楚我不做诊断，避免猜错设备。 |
| I1 | 3 | query_device_profile → query_defect_log → query_defect_log → query_defect_log → query_defect_log | ✓ | ✓ | ✓ | ✓ | CX-101 | 0 | 需要先确认设备信息：请问是哪一台？请提供设备编号。不确认清楚我不做诊断，避免猜错设备。 |
| I2 | 1 | query_device_profile → query_defect_log → query_defect_log → query_light_usage → query_defect_log → query_defect_log | ✓ | ✓ | ✗ | ✓ | - | 0 | ## 结论  CX-102 的误判**不是硬件问题，而是物料批次相关的模型/阈值问题**。问题高度集中在批次 **B20 |
| I2 | 2 | query_device_profile → query_defect_log → query_defect_log → query_defect_log → query_defect_log → query_light_usage | ✓ | ✓ | ✓ | ✓ | - | 0 | ## 结论  CX-102 的误判**不是某个工位的硬件问题，而是集中在物料批次 B20261105 上**，属于模型/ |
| I2 | 3 | query_device_profile → query_defect_log → query_defect_log → query_defect_log → query_defect_log → query_light_usage | ✓ | ✓ | ✓ | ✓ | - | 0 | ## 结论  CX-102 的误判**不是硬件问题，而是与物料批次强相关**：误判几乎全部集中在批次 **B202611 |