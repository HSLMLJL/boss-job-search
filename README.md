# Job Radar｜BOSS 直聘求职助手：职位搜索、岗位筛选、JD 导出与投递记录

[English](README.en.md) · [GitHub 仓库](https://github.com/HSLMLJL/boss-job-search) · [v0.1.0 Beta 固定代码版](https://github.com/HSLMLJL/boss-job-search/releases/tag/v0.1.0)

面向 BOSS 直聘求职者的本地搜索工作台：设置关键词和城市，收集岗位并按规则初筛；按选择尝试补全 JD（职位描述），导出 Excel、CSV 或 JSON 求职清单，记录“考虑、已投、面试”等进度。产品、运营、开发、设计、数据、销售等方向都可以替换关键词使用，AI 产品经理只是其中一种用法。

你可以这样使用：**搜索岗位 → 本地初筛 → 按需补 JD → 导出求职清单 → 准备简历和投递材料 → 记录进度**。收集到的 JD 用于人工对比岗位要求、选择真实经历和准备面试；项目当前不内置简历改写、AI 匹配评分、自动投递或自动发送消息。

> 使用自己的授权账号并遵守平台规则；项目不承诺覆盖平台全部岗位。

![Job Radar 搜索配置、完整 JD、岗位清单与导出（合成数据演示）](docs/images/job-radar-workspace.png)

*上图为合成数据演示。[无需安装，先看一份示例求职清单](examples/job-radar-sample.csv)。v0.1.0 Beta 已在 Windows 正常 Chrome 小范围验证：1 页任务保存 5 个岗位、获得 3 个非空 JD，并导出三种格式；这不是全平台覆盖结论。*

## 三种常见用法

| 你要做什么 | Job Radar 留下什么 |
| --- | --- |
| 多城市找岗位 | 多个关键词、城市与每条岗位的命中词、基础信息和链接 |
| 对照 JD 准备简历 | 初筛岗位的 JD、筛选原因与可导出的岗位要求清单；[人工准备材料的方法](docs/job-radar-resume-workflow.md) |
| 跟进申请 | `待看`、`考虑`、`已投`、`面试`、`不合适` 状态和备注 |

## 第一次使用：先体验，再连接真实搜索

首次需要安装本地运行环境，日常在界面里填选项。Windows + Chrome 已完成小范围验证，其他系统尚未完成同等验证。第一次使用 Python/uv 的用户从[逐步安装指南](docs/job-radar-quickstart.md)开始；已有环境的用户可按下面步骤操作，uv 可按需安装 Python。

1. 下载最新源码 [ZIP](https://github.com/HSLMLJL/boss-job-search/archive/refs/heads/main.zip)，解压并进入该文件夹。
2. 在终端运行：

   ```bash
   uv sync --python 3.13
   uv run python -m job_radar ui
   ```

3. 在本地工作台点击 **体验合成示例**。它不需要 BOSS 登录、Chrome 扩展或模型密钥。
4. 要真实搜索时运行 `uv run python -m job_radar setup`。按提示在 Chrome 的 `chrome://extensions` 开启开发者模式，选择“加载已解压的扩展程序”，选中命令打印的 `extension` 路径；随后在同一个 Chrome 登录 BOSS。回到工作台填写关键词、城市，选“初筛后补 JD”并开始搜索。

完成 `uv sync` 后，Windows 也可双击 `Start-Job-Radar.cmd`。完整步骤见 [Windows 本地安装](docs/job-radar-quickstart.md)。Beta 下载页提供固定的 v0.1.0 源码。

## 日常在界面里设置什么

| 项目 | 说明 |
| --- | --- |
| 搜索 | 多关键词、多城市分别搜索后合并；同一岗位保留命中关键词 |
| 本地初筛 | 薪资、经验、学历、行业、公司规模、融资阶段、岗位类型、福利、包含词和排除词；可选排除实习/小时工、保留或排除缺失信息。规则在采集列表与可选 JD 后本地应用，不承诺每项都发送为平台搜索条件 |
| JD 深度 | `none` 只留列表；`matched` 只为**本地初筛通过**的岗位尝试补 JD；`all` 为所有岗位尝试补 JD。`matched` 不是 AI 匹配评分 |
| 预算 | `pages` 是每个“关键词 × 城市”组合页数上限；`requests`、`details`、`jobs`、`seconds` 是本次启动或继续运行共用上限；结果展示覆盖范围和停止原因 |
| 导出与记录 | Excel、CSV、JSON 可多选；导出字段与匹配/全部/本次新增范围可选；岗位去重、本地保存首次/最近收录、变化、状态和备注 |
| 复用搜索 | 在界面保存任务模板，下次载入后可继续改关键词、城市和筛选；运行历史可回看 |

完整 JD 依赖同页可见的职位详情面板及职位 ID 核验。面板不支持、正文为空、预算不足或中断时会标记为未完成。运行时保持专用 Chrome 搜索页可见，不要手动改搜索或翻页；浏览器或扩展重启后本地清单仍在，但需新建搜索。

## 进阶：命令行与配置文件

日常使用不需要写 JSON。想保存或复制搜索条件时：

```bash
uv run python -m job_radar init --output my-search.json
uv run python -m job_radar run my-search.json
uv run python -m job_radar export RUN_ID
```

筛选、预算、导出字段和故障处理见[使用指南](docs/job-radar-guide.md)；示例见[通用岗位](examples/job-radar-general.json)与[运营岗位](examples/job-radar-operations.json)。`--data-dir PATH` 只隔离本地工作库，Chrome 登录状态仍由 Chrome 管理。

## 隐私、致谢与许可证

任务、岗位历史、备注和导出保存在本地。搜索、筛选、去重、状态和导出不依赖模型调用；外部 AI 用于简历准备是你的可选动作。本项目在 [boss-agent-cli](https://github.com/can4hou6joeng4/boss-agent-cli) 的 MIT 开源基础上开发，保留原项目版权和许可证。详见 [MIT License](LICENSE)。
