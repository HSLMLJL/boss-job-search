# Job Radar｜BOSS直聘求职助手：职位搜索、岗位采集、JD导出与投递记录

[English](README.en.md) · [GitHub 仓库](https://github.com/HSLMLJL/boss-job-search) · [Windows 便携版下载](https://github.com/HSLMLJL/boss-job-search/releases/tag/v0.2.0)

Job Radar 是面向普通求职者的 **BOSS直聘找工作工具与本地求职助手**，把职位搜索、职位采集、岗位筛选、JD 导出和投递记录放在一个工作台里，帮助你把招聘信息整理成可查看、可筛选、可跟进的投递清单。

想在 BOSS直聘上搜索职位、采集岗位？按自己的关键词和城市搜集岗位，集中查看职位、公司、薪资、地点与原始链接，再筛选、去重和导出。岗位采集不限职业方向，产品、运营、开发、设计、数据、销售等都可以替换关键词使用。

你可以只收集岗位列表，也可以选择为初筛通过的岗位或全部已收集岗位尝试获取完整 JD（职位描述）；导出格式、字段和范围均可选择，支持 Excel、CSV、JSON。求职管理通过手动投递记录和备注完成，可标记“待看、考虑、已投、面试”等进度。

Windows 用户下载便携包、解压后即可启动，无需编程；首次真实采集按引导加载 Chrome 扩展并登录 BOSS直聘。

**搜索岗位 → 本地初筛 → 按需补 JD → 导出求职清单 → 准备简历和投递材料 → 记录进度**

**[下载 Windows 便携版](https://github.com/HSLMLJL/boss-job-search/releases/download/v0.2.0/Job-Radar-0.2.0-Windows-x64.zip)**（约 16 MB，无需安装 Python 或 uv） · [先看一份示例求职清单](examples/job-radar-sample.csv) · [图文安装指南](docs/job-radar-quickstart.md)

![Job Radar 搜索配置、完整 JD、岗位清单与导出（合成数据演示）](docs/images/job-radar-workspace.png)

*上图为合成数据演示。v0.2.0 Windows 便携包已在正常 Chrome 小范围验证：1 页任务保存 5 个岗位、获得 3 个非空 JD，并导出三种格式；这不是全平台覆盖结论。*

## 能搜索和采集什么

| 选项 | 你可以怎么设置 |
| --- | --- |
| 搜索关键词 | 自己填写一个或多个职位词，例如产品经理、运营、Java、设计、数据分析或销售；不限 AI 产品经理 |
| 搜索城市 | 选择多个城市，分别搜索后合并；同一岗位保留命中关键词 |
| 岗位列表 | 收集页面可见的职位、公司、薪资、地点、经验、学历等基础信息与岗位链接；缺失字段保留为缺失 |
| 完整 JD | 三选一：只采集列表（`none`）；只为本地初筛通过的岗位尝试补 JD（`matched`）；为全部已采集岗位尝试补 JD（`all`） |
| 采集数量与时间 | 页数上限按每个“关键词 × 城市”组合计算；请求数、详情数、岗位数和运行秒数是本次启动或继续运行共用的上限，结果会展示覆盖范围与停止原因 |

`matched` 表示通过你设置的本地筛选规则。JD 是否取得取决于页面详情和本次采集上限，未取得的会标记为未完成。

## 怎样筛选岗位、导出 JD 和管理投递清单

| 选项 | 你可以怎么设置 |
| --- | --- |
| 岗位筛选 | 薪资、经验、学历、行业、公司规模、融资阶段、岗位类型、福利、包含词与排除词；可选排除实习/小时工，以及保留或排除信息缺失的岗位 |
| 导出格式 | Excel、CSV、JSON 可多选；有内容的 JD 可随选定字段导出，方便逐岗查看和对照要求准备材料 |
| 导出内容 | 字段与范围可选：初筛通过的岗位、全部已采集岗位，或本次新增岗位 |
| 去重与变化 | 同一岗位去重，保存首次/最近收录时间和岗位变化 |
| 投递记录 | 手动记录 `待看`、`考虑`、`已投`、`面试`、`不合适` 及备注，方便后续跟进 |
| 复用搜索 | 保存任务模板，下次载入后继续修改关键词、城市和筛选条件；运行历史可回看 |

筛选规则在列表与可选 JD 采集后于本地应用，不代表每项条件都会发送到 BOSS 搜索。收集到的 JD 可用于人工对比岗位要求、选择真实经历和准备面试，见[从岗位 JD 到简历准备](docs/job-radar-resume-workflow.md)。

## 第一次使用：Windows 便携包优先

下载 [Job-Radar-0.2.0-Windows-x64.zip](https://github.com/HSLMLJL/boss-job-search/releases/download/v0.2.0/Job-Radar-0.2.0-Windows-x64.zip)（约 16 MB）。包内自带官方 Python 运行环境与依赖，**无需先安装 Python 或 uv**。

1. 右键 ZIP 选择“全部解压缩”，进入解压后的文件夹。**不要从压缩包预览窗口直接启动，也不要下载 GitHub 的 Source code ZIP 来代替便携包。**
2. 双击包内 `Start-Job-Radar.cmd`。本地工作台会打开；便携包默认把任务、导出和备注保存到 `%LOCALAPPDATA%\Job-Radar`。
3. 先点 **体验合成示例**，无需登录 BOSS 或加载扩展。
4. 需要真实搜索时，按界面“连接 BOSS”引导：在 Chrome 手动开启开发者模式、加载包内 `extension` 文件夹、登录 BOSS，再刷新连接状态。扩展权限不会被自动修改。

完整图文步骤见 [Windows 便携包快速开始](docs/job-radar-quickstart.md)。

## 常见问题

### 怎样在 BOSS直聘上采集岗位、整理找工作清单？

连接自己的 Chrome 和 BOSS 账号，填写关键词与城市，选择筛选条件、是否采集完整 JD、采集上限与导出内容，再开始搜索。完成后查看岗位清单与停止原因，将准备投递的岗位导出或标为“考虑”。

### 搜集岗位一定要采集完整 JD 吗？

不用。只想先收集岗位信息，可以选“只采集列表”；需要进一步比较职责和要求时，再选择“初筛后补 JD”或“全部补 JD”。三种方式均受本次采集上限约束。

### 能把 BOSS 岗位和职位描述导出到 Excel 吗？

可以。选择 Excel、需要的字段与导出范围，也可以同时导出 CSV 和 JSON。完整职位描述以实际取得的 JD 为准，空缺不会被当作已采集成功。

### 能让 AI 助手去 BOSS 搜集岗位，供我准备投递吗？

可以让具备本机命令执行能力的 AI 助手通过 CLI 创建搜索任务、收集岗位并读取导出结果。首次连接仍需手动加载扩展并登录；普通用户直接用界面即可。具体命令和参数见[AI 助手与命令行调用指南](docs/job-radar-agent-usage.md)。

### 这是 BOSS 爬虫吗？会自动投递简历吗？

它提供基于本人已登录 Chrome 页面自动化的职位抓取与岗位采集能力（job scraper），并将结果整理为本地求职清单。当前不提供自动投递、自动发消息、简历改写或 AI 匹配评分；投递状态由用户手动记录。

## 进阶：源码安装

首次需要安装本地运行环境，日常在界面里填选项。Windows + Chrome 已完成小范围验证，其他系统尚未完成同等验证。第一次使用 Python/uv 的用户从[逐步安装指南](docs/job-radar-quickstart.md)开始；已有环境的用户可按下面步骤操作，uv 可按需安装 Python。

1. 下载最新源码 [ZIP](https://github.com/HSLMLJL/boss-job-search/archive/refs/heads/main.zip)，解压并进入该文件夹。
2. 在终端运行：

   ```bash
   uv sync --python 3.13
   uv run python -m job_radar ui
   ```

3. 在本地工作台点击 **体验合成示例**。它不需要 BOSS 登录、Chrome 扩展或模型密钥。
4. 要真实搜索时运行 `uv run python -m job_radar setup`。按提示在 Chrome 的 `chrome://extensions` 开启开发者模式，选择“加载已解压的扩展程序”，选中命令打印的 `extension` 路径；随后在同一个 Chrome 登录 BOSS。回到工作台填写关键词、城市，选“初筛后补 JD”并开始搜索。

完成 `uv sync` 后，源码用户也可双击仓库内的 `Start-Job-Radar.cmd`。普通用户直接使用上方便携包即可。

## 进阶：命令行与配置文件

日常使用不需要写 JSON。想保存或复制搜索条件时：

```bash
uv run python -m job_radar init --output my-search.json
uv run python -m job_radar run my-search.json
uv run python -m job_radar export RUN_ID
```

筛选、预算、导出字段和故障处理见[使用指南](docs/job-radar-guide.md)；示例见[通用岗位](examples/job-radar-general.json)与[运营岗位](examples/job-radar-operations.json)。`--data-dir PATH` 只隔离本地工作库，Chrome 登录状态仍由 Chrome 管理。若由 AI 或脚本创建任务，见[命令与 AI 助手调用](docs/job-radar-agent-usage.md)。

## 使用边界与隐私

使用自己的授权账号并遵守平台规则；项目不承诺覆盖平台全部岗位。完整 JD 依赖同页可见的职位详情面板及职位 ID 核验。面板不支持、正文为空、采集上限不足或中断时会标记为未完成。

运行时保持专用 Chrome 搜索页可见，不要手动改搜索或翻页；浏览器或扩展重启后本地清单仍在，但需新建搜索。任务、岗位历史、备注和导出保存在本地；搜索、筛选、去重、状态和导出不依赖模型调用，外部 AI 用于简历准备是可选动作。

## 致谢与许可证

本项目在 [boss-agent-cli](https://github.com/can4hou6joeng4/boss-agent-cli) 的 MIT 开源基础上开发，保留原项目版权和许可证。详见 [MIT License](LICENSE)。
