# Job Radar｜BOSS 直聘职位搜索与求职助手

[English](README.en.md)

**获取项目：**[GitHub 仓库](https://github.com/HSLMLJL/boss-job-search) · [v0.1.0 Beta 下载页](https://github.com/HSLMLJL/boss-job-search/releases/tag/v0.1.0)

一个帮你在 BOSS 直聘上找工作的开源工具：自定义关键词和城市，按条件筛选招聘信息，按需采集完整 JD，并导出 Excel 清单持续跟进。

它把在 BOSS 直聘上反复搜索、翻页、复制岗位信息的过程，整理成一个可以配置、回看和持续跟进的本地求职工作台。

你决定找什么岗位、在哪些城市找、采集到什么程度；Job Radar 负责把本次结果、完整 JD（可选）、筛选依据和你的求职进度留在本地。它服务于普通求职者，不限行业或岗位类别；AI 产品经理只是可保存的一种搜索模板。

> 当前数据源：BOSS 直聘。使用前请遵守平台规则和自己的账号权限；本项目不承诺穷尽平台全部岗位，也不替你自动投递或发送消息。

![Job Radar 搜索配置、完整 JD、岗位清单与导出（合成数据演示）](docs/images/job-radar-workspace.png)

*界面中的岗位为合成演示数据。*

## 你能用它做什么

| 场景 | 你可以配置 | Job Radar 会留下什么 |
| --- | --- | --- |
| 找岗位 | 多个关键词、多个城市；如“产品经理”“用户运营”“数据分析师” | 每个岗位命中的关键词、基础信息和链接 |
| 控制 JD 采集 | `不采集`、`仅对筛选通过的岗位采集`、`全部采集` | 完整 JD（如选择采集）和 JD 采集状态 |
| 缩小范围 | 薪资、经验、学历、行业、公司规模、融资阶段、职位类型、福利 | 包含/排除词和每条岗位的筛选依据 |
| 控制本次任务 | 每个“关键词 × 城市”组合的最大页数；整次任务的请求数、JD 数、岗位数和运行时长 | 任务进度、已完成页面和停止原因 |
| 导出与跟进 | Excel、CSV、JSON；选择字段；仅导出匹配、全部或本次新增 | 首次/最近收录时间、变化、个人进度和备注 |

它不是把“首次在你的本地库里看到”写成“平台刚发布”：两个时间字段会分开保存。一次任务在上限、页面状态或网络问题前结束时，工作区会保留已完成页面和停止原因；不能据此推断没有更多岗位。

## 先打开本地界面

普通用户只需准备 Python 3.10+、[uv](https://docs.astral.sh/uv/) 和日常使用的 Chrome。打开 [v0.1.0 Beta 下载页](https://github.com/HSLMLJL/boss-job-search/releases/tag/v0.1.0)，下载 Source code（ZIP）并解压；进入解压后的项目目录，执行：

```bash
uv sync
```

首次使用时，先启动本地 Browser Bridge：

```bash
uv run python -m job_radar setup
```

命令会打印本仓库 `extension` 文件夹的**绝对路径**。打开 Chrome 的 `chrome://extensions`，开启“开发者模式”，选择“加载已解压的扩展程序”，再选中这个 `extension` 文件夹。随后在同一个日常 Chrome 中登录 BOSS 直聘。这是一次性的本地授权；登录状态始终由 Chrome 维护，无需复制 Cookie，也无需运行 `boss login`。

Windows 用户可以双击 `Start-Job-Radar.cmd` 启动本地界面。也可以在终端执行：

```bash
uv run python -m job_radar ui
```

它会默认打开本机 `http://127.0.0.1:8766`，并启动本地 Bridge。界面只在你的电脑上运行，用来创建任务、查看历史、修改进度和导出结果。安装生成的短命令 `job-radar` 也可用；若 Windows 策略拦截该入口，继续使用这里的模块式命令即可。

## 再体验命令行与合成演示

```bash
uv run python -m job_radar demo
```

`demo` 使用合成岗位数据，不登录、不请求 BOSS，也不需要 Chrome 或任何模型密钥。

## 开始一次真实搜索

先按上面的首次设置把扩展加载到日常 Chrome，并在 Chrome 里登录 BOSS 直聘；然后生成一份任务配置，再按需要修改：

```bash
uv run python -m job_radar init --output my-search.json
uv run python -m job_radar run my-search.json
```

例如，一个“上海/杭州产品岗位，优先补全通过筛选岗位的 JD，导出 Excel”的任务可以写成：

```json
{
  "name": "华东产品岗位",
  "keywords": ["产品经理", "用户研究"],
  "cities": ["上海", "杭州"],
  "details": "matched",
  "filters": {
    "experience": "3-5年",
    "include": ["AI", "增长"],
    "exclude": ["销售"],
    "exclude_internships": true,
    "unknown": "keep"
  },
  "limits": {"pages": 5, "requests": 50, "details": 30, "jobs": 300, "seconds": 900},
  "export": {"formats": ["xlsx"], "scope": "matched"}
}
```

完整可复制配置见 [通用示例](examples/job-radar-general.json) 和 [运营岗位示例](examples/job-radar-operations.json)。字段含义和筛选取舍见[使用指南](docs/job-radar-guide.md)。

## 每次搜索时，你实际控制的选项

### 1. 搜索范围

- `keywords`：多个关键词会分别搜索并合并结果。每条记录会保留命中的关键词，方便回看“它是从哪一个搜索词来的”。
- `cities`：可选多个 BOSS 支持的城市。
- `filters`：可按薪资、经验、学历、行业、规模、融资阶段、职位类型和福利筛选；`include`、`exclude` 是在已采集文本上的进一步判断。
- 当前版本会先从页面采集列表和可选 JD，再在本地应用这些筛选规则；不承诺每个参数都会作为平台页面的搜索条件发送。
- `unknown`：岗位缺少相关字段时，选择 `keep` 保留以便人工看，或 `exclude` 让它随筛选排除。

### 2. 采集内容和预算

- `details: none`：只保留列表信息，最快。
- `details: matched`：先收集列表，再只补全通过筛选的岗位 JD；日常找工作推荐使用。
- `details: all`：为所有结果尝试补全 JD，适合小范围深入研究。详情接口空返回、预算不足或任务中断会保留对应 JD 状态，不会被写成“已采集完整 JD”。
- `limits.pages`：每个“关键词 × 城市”组合可访问的最大页数；`requests`、`details`、`jobs`、`seconds` 则是本次启动或继续运行共用的上限。共同预算可能在后续组合开始前用尽；任务会如实记录，不把未访问范围当成零结果。

### 3. 输出和跟进

- `export.formats`：选 Excel (`xlsx`)、CSV 或 JSON，可多选。
- `export.fields`：选择导出哪些列，例如 JD、技能、筛选依据、首次收录时间、个人状态与备注。
- `export.scope`：导出筛选通过的岗位 (`matched`)、本次所有岗位 (`all`) 或本次第一次收录的岗位 (`new`)。`new` 按首次收录判断，包含本次新收录但未通过筛选的岗位，便于复查规则。
- 个人状态：`待看`、`考虑`、`已投`、`面试`、`不合适`；状态和备注按岗位保留，下一次出现同一岗位时会继承。

## 日常使用流程

1. 在 UI 新建或复用一个任务模板，确认关键词、城市、筛选、JD 策略和预算。
2. 运行任务，先看任务是否完成以及覆盖范围，再打开结果判断岗位。
3. 对值得跟进的岗位填写状态和备注；这比重新搜索时凭记忆判断更可靠。
4. 导出 Excel 进行比较、投递清单整理或与他人协作。
5. 下次沿用模板运行；系统用稳定岗位标识追踪首次收录、最近收录和信息变化。

如运行被用户中断、遇到平台状态或达到预算，可保留本地清单并导出。页面仍保持在本次专用搜索标签页、且扩展检查点仍在时，才可以尝试用运行编号继续；继续会使用一段新的预算窗口：

```bash
uv run python -m job_radar resume RUN_ID
uv run python -m job_radar export RUN_ID
```

默认本地数据目录为 `~/.boss-agent/job-radar`。如需将测试和正式数据分开，可在命令前加入 `--data-dir PATH`；它只管理 Job Radar 的本地工作库，Chrome 的登录状态仍由 Chrome 自己维护。

## 边界与隐私

- Job Radar 不自动代替你做投递、打招呼或发送消息；最终判断和平台操作由你完成。
- 本地工作库保存任务、岗位历史、状态和备注。导出文件请按自己的隐私要求保存和分享。
- 平台页面、账号登录状态、网络与访问限制会影响一次任务的覆盖度。请以任务状态和范围记录为准。
- 真实搜索使用扩展绑定的专用 Chrome 搜索标签页。运行时请让该页保持可见，不要同时手动切换搜索条件或翻页。浏览器或扩展重启后，已保存的清单和页面仍保留，但页面检查点会失效；任务会明确停止，需要新建一次搜索，而不是承诺跨重启续跑。
- 完整 JD 依赖该页面中可见的职位详情面板，并逐个岗位标识核验。面板不支持或正文为空时，会保留“未完成”状态。
- AI 分析、原有 CLI 和 MCP 接口属于进阶能力；Job Radar 的基础采集、筛选、去重、状态和导出不依赖模型调用。

## 文档

- [使用指南：配置、筛选、JD 与历史](docs/job-radar-guide.md)
- [通用岗位任务示例](examples/job-radar-general.json)
- [运营岗位任务示例](examples/job-radar-operations.json)
- [版本说明](docs/job-radar-release-notes.md)

## 致谢

本项目在 [boss-agent-cli](https://github.com/can4hou6joeng4/boss-agent-cli) 的 MIT 开源基础上开发，保留原项目的版权和许可证；Job Radar 聚焦于面向求职者的任务配置、持续追踪与本地工作区体验。

## 许可证

本仓库使用 [MIT License](LICENSE)。
