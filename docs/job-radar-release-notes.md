# Job Radar 版本说明

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

建议使用 [github-metadata.json](github-metadata.json) 中的这一组元数据：

- **Repository name / slug**：`boss-job-search`
- **About description（实际只设置这一条）**：BOSS直聘找工作工具｜职位搜索、岗位采集与求职助手：自定义关键词、多城市筛选、可选完整JD、Excel/CSV/JSON导出与进度记录。BOSS Zhipin job search tool, job scraper and job description exporter.
- **Topics**：`boss-zhipin`、`job-search`、`job-search-tool`、`job-scraper`、`job-description`、`excel-export`、`career-tools`、`python`、`local-first`

这条 About 覆盖“BOSS 直聘、找工作、职位搜索、岗位采集、JD、Excel 导出”，也表达 `boss zhipin job search`、`job scraper`、`job description exporter` 的用户意图。这些词描述实际能力，不承诺不存在的跨平台能力或自动投递功能。

用未登录窗口或普通 GitHub 搜索页做下列检查，并给索引留出正常生效时间：

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
