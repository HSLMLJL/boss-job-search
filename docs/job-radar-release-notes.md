# Job Radar 版本说明

## v0.2.0 Windows 便携版 Beta（2026-10-21）

下载 `Job-Radar-0.2.0-Windows-x64.zip`，全部解压后双击 `Start-Job-Radar.cmd`。包内自带官方 CPython 3.13.13 运行环境与依赖，无需安装 Python 或 uv；任务、导出和备注默认放在 `%LOCALAPPDATA%\Job-Radar`。

首次真实搜索仍需要手动在 Chrome 加载包内扩展并登录 BOSS，界面提供扩展目录、复制路径与连接检查。新增示例体验入口、应用退出按钮、重复启动复用、端口占用自动避让与退出保存。包内命令行也可供 AI 助手或脚本调用，见[调用指南](job-radar-agent-usage.md)。

### 本次验收

- 同一 Windows 主机上使用全新中文/空格目录、独立本地数据与不含 Python/uv 的 PATH 启动便携包；包内 Python 签名为有效的 Python Software Foundation 签名。
- 两关键词 × 两城市的合成任务保存 6 个去重岗位；关键词、城市、JD、状态、备注和三格式导出流程可用。
- 包内真实搜索读取一页 15 条列表，按配置保存 5 岗、取得 3 条非空 JD，4 次采集调用后按预算停止；Excel/CSV 逐字段一致，JSON 与 JD 原文一致。
- 重复启动、端口占用自动避让、退出重开后历史和备注保留；包内 CLI 的合成任务无需宿主 Python/uv。
- 93 项自动化测试通过；退出保留原 Chrome 与已有 Bridge，缺失扩展时仍可使用示例与本地记录。

本次是开发者按新人步骤执行的隔离目录验收，复用了已有 Chrome 扩展与登录；未声称已完成纯净虚拟机、新用户首次手动安装扩展或独立陌生用户测试。Windows 安全策略仍可能限制运行；不会要求关闭系统防护。

## v0.1.0 Beta（2026-10-21）

这一版把原先分散在搜索、采集和整理流程中的关键选择汇总为一份任务配置，并提供本地浏览器界面与命令行入口。

项目地址：[HSLMLJL/boss-job-search](https://github.com/HSLMLJL/boss-job-search)。下载请使用 [v0.1.0 Beta release](https://github.com/HSLMLJL/boss-job-search/releases/tag/v0.1.0) 页面中的 Source code（ZIP）。

### 本次小范围验证

2026-10-21 在 Windows 的正常 Chrome 会话中完成一次小范围真实搜索：任务主动设为 1 页预算，保存 5 个岗位，本地筛选保留 4 个，读取到 3 个非空完整 JD，并成功导出 Excel、CSV、JSON 三种格式。

这只是一次受控 smoke 验证，用来确认 Chrome Bridge、单页采集、JD 面板与导出链路可以协同工作；它不代表全部平台岗位覆盖，也不是跨城市、跨浏览器或长期稳定性的完整回归结论。

### 已提供

- 多关键词、多城市的 BOSS 岗位搜索任务
- `none`、`matched`、`all` 三种完整 JD 采集策略
- 岗位条件、福利、包含词、排除词以及缺失信息处理
- 每个“关键词 × 城市”组合的页数预算，以及整次任务共用的请求数、详情数、岗位数和运行时长预算
- Excel、CSV、JSON 导出；导出范围和字段选择
- 本地任务模板、已完成页面记录；在页面检查点仍存在时可继续运行
- 基于稳定岗位标识的历史去重、首次/最近收录和信息变化
- 人工状态与备注，并在后续再次看到同一岗位时继承
- 无账号、无平台请求的合成数据演示

### 当前范围

- Job Radar 当前面向 BOSS 直聘的求职端使用场景。
- “首次收录”仅描述本地工作区第一次看到岗位，不等同于平台发布时间。
- 任务受配置预算、平台可见页面、网络和登录状态影响；结果中会保留任务状态，不能把未完成任务当作完整市场统计。
- 自动投递、自动沟通和替代人工决策不属于这个工作区的基础流程。
- 真实搜索复用用户日常 Chrome 中的登录状态，通过本地 Browser Bridge 固定专用搜索页采集列表，并从同页详情面板按职位 ID 读取 JD；不需要复制 Cookie 或启用独立 Cookie 浏览器路径。
- 运行时需保持专用搜索页可见，避免手动切换搜索或翻页。浏览器或扩展重启后，已保存的本地结果仍在，但页面检查点失效，任务会停止并要求新建搜索；不承诺跨重启续跑。
- 当前筛选在列表/JD 采集后本地应用，不承诺全部筛选字段都会发送为平台搜索参数；空正文或不支持的详情面板保持 JD 未完成。

### GitHub 发布元数据与发现性检查

GitHub 的默认仓库搜索会检索仓库名称、简介和 topics；README 正文需要通过 `in:readme` 才会被专门检索。当前公开仓库为 [HSLMLJL/boss-job-search](https://github.com/HSLMLJL/boss-job-search)。可用下表检查索引，不承诺固定排名。依据见 [GitHub 官方仓库搜索说明](https://docs.github.com/en/search-github/searching-on-github/searching-for-repositories)。

当前 About 和 topics 以 [github-metadata.json](github-metadata.json) 为准。介绍覆盖职位搜索、职位采集、岗位筛选、JD 导出、投递记录和求职管理；“简历准备”指使用导出 JD 作为资料，不表示内置简历改写或匹配评分。

- **Repository name / slug**：`boss-job-search`
- **About / Topics**：使用上述 JSON 的 `description` 与 `topics` 字段，保持公开信息一致。

2026-10-21 首发后按陌生求职者视角修订 README，补充[逐步安装指南](job-radar-quickstart.md)、[合成清单预览](../examples/job-radar-sample.csv)和[JD 到简历准备的方法](job-radar-resume-workflow.md)。本次只更新文档、示例与元数据，v0.1.0 的代码和原发行资产保持原版本。

发现性检查应先用未知项目名时会输入的词，如 `BOSS 岗位筛选`、`BOSS 职位采集`、`BOSS 求职助手`，记录实际排名或未出现的范围。下面带限定符的查询只检查索引，不能代替陌生用户的自然搜索。给索引留出正常生效时间，不将一次排名当成长期效果。

| 新人可能会搜什么 | GitHub 检查查询 | 应看到什么 |
| --- | --- | --- |
| 已知项目名 | `boss-job-search in:name` | 本仓库名称 |
| BOSS 直聘找工作工具 | `BOSS 直聘 in:description` | About description |
| BOSS Zhipin job search | `BOSS Zhipin job search in:description` | About description |
| BOSS 招聘平台相关工具 | `boss-zhipin in:topics` | 本仓库 topic |
| 导出岗位 JD 到 Excel | `excel-export in:topics` | 本仓库 topic |

若仓库保留为 GitHub fork，默认搜索可能不展示它；检查时附加 `fork:true`。若以独立仓库发布，则不需要这个限定。

### 下一步会围绕真实使用继续完善

- 让任务配置、运行状态和结果解释更直观
- 用实际用户反馈持续校正筛选、JD 补全与导出体验
- 在不牺牲本地可控和可解释性的前提下完善进阶分析能力
