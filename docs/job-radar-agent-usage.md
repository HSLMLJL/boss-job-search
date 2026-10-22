# 用 Job Radar 为投递整理 BOSS 岗位：命令与 AI 助手调用

当你让 AI 助手“去 BOSS 搜集岗位供我投递”时，可让它帮你生成和执行 **Job Radar 的本地任务配置**，但真实搜索仍需你在 Chrome 登录 BOSS、手动加载扩展，并由你决定投递。

## 本机运行与 AI 额度

命令启动后，岗位搜索、列表与完整 JD 采集、规则筛选、去重和导出由本机程序执行，不需要模型 API Key，也不按岗位条数消耗模型 token。完整 JD 会增加逐条打开详情面板、等待加载和核对岗位的时间；这段等待本身不需要 AI 持续推理。日常任务可选择 `matched`，只为初筛通过的岗位补 JD。

AI 助手生成配置、查看进度、阅读结果或分析 JD 属于另外的 AI 交互，仍按其服务规则消耗额度或计费。`requests`、`details`、`seconds` 等预算是采集数量和时长上限，不是模型付费额度。采集期间保持本地程序运行和专用 Chrome 搜索页可见，无需让 AI 逐条操作岗位；遇到登录或安全验证仍需人工处理。

## 可执行流程

1. 在 Job Radar 的本地 UI 完成“连接 BOSS”，或用源码方式先运行 `uv run python -m job_radar setup` 并按提示加载扩展。
2. 保存下面的 JSON 为 `my-search.json`，替换关键词和城市。
3. 在源码环境执行：

   ```bash
   uv run python -m job_radar run my-search.json
   ```

4. 终端会打印运行 ID 与导出目录。按需重新导出：

   ```bash
   uv run python -m job_radar export RUN_ID
   ```

便携包同样可以直接执行命令，不必为 AI 或脚本任务另装源码版和 uv。它与 UI 共用同一份本地数据；同一时刻只运行一个采集任务。AI 可以帮你拟定关键词、筛选和导出字段，也可以在你授权的本机环境中执行这些本地命令和整理导出的结果；登录、扩展加载、安全验证及最终投递决定仍由用户完成。

## 便携包中的 PowerShell 调用

先把 `my-search.json` 放在便携包根目录（与 `runtime` 文件夹同级），在该目录打开 PowerShell：

```powershell
& .\runtime\python.exe -m job_radar --data-dir "$env:LOCALAPPDATA\Job-Radar" run .\my-search.json
& .\runtime\python.exe -m job_radar --data-dir "$env:LOCALAPPDATA\Job-Radar" export RUN_ID
```

这里的 `RUN_ID` 是运行命令完成后输出的运行 ID。`--data-dir` 放在 `job_radar` 后、`run` 或 `export` 子命令前，因此和便携包 UI 读写同一份任务、历史和导出数据。不要在 UI 正在采集时再启动命令行采集。

要先离线检查完整流程，可用同一份配置运行合成示例：

```powershell
& .\runtime\python.exe -m job_radar --data-dir "$env:LOCALAPPDATA\Job-Radar" run .\my-search.json --demo
```

`--demo` 不登录、不访问 BOSS，导出的岗位为合成数据。

## 最小任务配置

```json
{
  "name": "上海和杭州的产品岗位",
  "keywords": ["产品经理", "增长产品"],
  "cities": ["上海", "杭州"],
  "details": "matched",
  "filters": {
    "include": ["增长"],
    "exclude": ["销售"],
    "unknown": "keep"
  },
  "limits": {"pages": 1, "requests": 20, "details": 10, "jobs": 50, "seconds": 300},
  "export": {"formats": ["json", "xlsx"], "fields": ["title", "company", "city", "salary", "description", "url", "detail_status", "filter_reason", "status", "notes"], "scope": "matched"}
}
```

`keywords` 与 `cities` 都是列表；每个关键词×城市组合分别使用 `pages` 页数上限，其余预算由整次运行共用。`details` 只能是 `none`、`matched`、`all`：`matched` 只为本地筛选规则通过的岗位尝试补 JD，不是 AI 匹配。JSON 导出便于 AI 或脚本读取，但其中的 JD、备注和岗位链接可能包含个人求职信息，分享前先自行检查。

## 使用独立本地库

`--data-dir` 是全局选项，必须放在命令和子命令之间：

```bash
uv run python -m job_radar --data-dir "D:\\Job-Radar-Data" run my-search.json
uv run python -m job_radar --data-dir "D:\\Job-Radar-Data" export RUN_ID
```

它只改变任务、历史和导出的位置，不迁移 Chrome 的登录状态。默认源码数据目录是 `~/.boss-agent` 下的 Job Radar 工作库；v0.2.0 便携包将默认使用 `%LOCALAPPDATA%\Job-Radar`。

## 给 AI 助手的边界

可以要求 AI：根据你的求职方向提出关键词、城市、包含/排除词；生成上述 JSON；阅读已导出的 JSON 并帮你整理 JD 要求。

不要把它描述为自动投递工具：Job Radar 不会自动发送消息、投递岗位、修改简历，也没有声明为 MCP 服务。平台登录、安全验证、最终判断和投递都由你完成。
