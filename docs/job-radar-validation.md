# Job Radar：实际使用与测试记录

[返回产品介绍](../README.md) · [English summary](#english-summary)

更新日期：2026-10-22。日常自用经历说明产品从哪里来，发布包测试记录说明某个具体版本检查过哪些流程。

## 持续自用与产品经验

Job Radar 来自开发者持续多日的真实求职实践，日常流程累计筛选数万条岗位记录，并持续用于岗位搜集、筛选和投递清单整理。这些经验推动了多关键词、多城市、可选完整 JD、筛选、去重、导出和投递记录等功能的整理与改进。

“数万条”描述日常求职过程中的累计筛选记录和使用经验，不作为单次任务容量或去重后的独立岗位数。它也不同于下面专门针对 v0.2.0 便携包的一次发布抽查。

## 浏览器验证情况

| 浏览器与环境 | 已有使用或验证记录 | 当前使用指引 |
| --- | --- | --- |
| Windows + Chrome | 已完成 v0.2.0 便携包的启动、连接与真实采集流程验收 | 当前推荐环境，快速开始和界面连接说明以 Chrome 为例 |
| Microsoft Edge | 开发者确认，此前也完成过实际使用验证 | 保留既有使用验证结论；具体浏览器版本与配置需按实际环境确认 |

Edge 的记录来自此前实际使用，不表示所有 Edge 版本和配置都已逐项测试。v0.2.0 本页的可复核发布包验收环境为 Windows + Chrome；其他操作系统尚未完成同等验收。

## v0.2.0 Windows 便携包发布验收

测试日期：2026-10-21。版本：[v0.2.0](https://github.com/HSLMLJL/boss-job-search/releases/tag/v0.2.0)。

### 安装与日常操作

- 在新的中文/空格解压目录、独立数据目录及不含宿主 Python/uv 的 PATH 下启动包内运行环境。
- 检查端口占用时自动避让、重复启动复用实例、界面打开与 Chrome 连接流程。
- 运行含两个关键词、两个城市的合成示例，检查筛选、去重和 Excel、CSV、JSON 导出。
- 检查备注和投递状态保存、退出后重新打开的历史保留，以及包内命令行调用。
- 当时的离线回归测试 93 项通过；这些是功能检查，不是 93 名独立用户的使用统计。

### 指定预算下的真实账号抽查

| 检查项 | 当次结果 |
| --- | --- |
| 读取范围 | 1 个列表页，页面返回 15 条岗位 |
| 保存岗位 | 按本次设定上限保存 5 条 |
| 完整 JD | 按本次设定上限取得 3 条非空 JD |
| 采集调用与停止 | 列表和详情合计 4 次采集调用，达到设定预算后停止 |
| 导出 | Excel、CSV、JSON 三种格式成功；Excel/CSV 选定字段及 JSON 中 JD 全文核对一致 |

**5 条岗位、3 条 JD 是此次发布抽查主动设置的上限，不是产品的采集能力上限，也不代表全部日常使用量。** 抽查目的是确认便携包能完成真实采集和导出流程。

测试使用同一台 Windows 电脑上的隔离目录与环境，复用了已安装的 Chrome 扩展和登录状态；没有把它登记为纯净虚拟机或陌生用户的独立验收。真实岗位明细、账号信息和登录数据不随测试记录公开。

## English summary

Job Radar comes from ongoing personal job-search use, with experience screening tens of thousands of job records across multiple days. It remains in everyday use for collecting, filtering, and organizing jobs and application lists. This is cumulative workflow experience, not a claim about unique job count or the capacity of one run.

Chrome is the currently recommended browser and was used for the documented v0.2.0 Windows release check. Microsoft Edge was also validated in earlier firsthand use. That earlier experience is retained without treating every Edge version and configuration as separately tested. Other operating systems have not received equivalent release validation.

The 2026-10-21 portable-package checks covered fresh extraction, an isolated data directory and PATH without host Python/uv, startup and port handling, the synthetic demo, exports, notes and status persistence, reopening, and the bundled CLI. The recorded offline regression suite passed 93 tests.

The real-account release check read one page containing 15 listings, saved five jobs, collected three non-empty JDs, and exported Excel, CSV, and JSON with content checks. **Five jobs and three JDs were deliberately chosen test limits, not the tool's capacity or the total extent of everyday use.** The run stopped at its configured budget after four collection calls. It used an existing Chrome extension and login on the same Windows host with isolated directories; it was not a clean-VM or independent-user test.
