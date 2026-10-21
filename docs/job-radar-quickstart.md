# 第一次使用：从下载到导出求职清单

这份指南面向第一次从 GitHub 下载工具的求职者。日常使用在网页界面里填选项，不需要写代码或 JSON；首次安装仍需要执行几条命令。当前已完成 Windows + Chrome 小范围真实验证，其他系统尚未完成同等验证。

## 先看看最终会得到什么

- [界面预览](images/job-radar-workspace.png)：填写关键词、城市和筛选，查看岗位与 JD，记录个人进度。
- [合成求职清单 CSV](../examples/job-radar-sample.csv)：在 GitHub 可直接预览，也可下载后用 Excel 打开。全部岗位均为演示数据。
- [怎样把 JD 用于简历和面试准备](job-radar-resume-workflow.md)：介绍导出以后的使用方法。

这不是 Chrome 商店里点击一次就装好的独立插件。它由本地工作台和 Chrome 扩展配合运行；扩展负责连接 BOSS 页面，本地工作台负责选项、记录与导出。

## 1. 下载并解压

下载[当前源码 ZIP](https://github.com/HSLMLJL/boss-job-search/archive/refs/heads/main.zip)，右键选择“全部解压缩”。进入解压后的项目文件夹，直到看到 `pyproject.toml`、`Start-Job-Radar.cmd` 和 `extension` 文件夹。

不要在 ZIP 压缩包预览窗口里直接运行。你也可以在[版本下载页](https://github.com/HSLMLJL/boss-job-search/releases/tag/v0.1.0)取得固定的 v0.1.0 Beta 源码包；`.whl` 和 `.tar.gz` 主要供 Python 开发者使用。

## 2. 准备运行环境

需要联网安装依赖和一个可正常使用的 Chrome。已有 uv 的用户直接继续。

Windows 可打开 PowerShell，按 [uv 官方安装说明](https://docs.astral.sh/uv/getting-started/installation/#winget)执行：

```powershell
winget install --id=astral-sh.uv -e
```

安装后关闭并重新打开终端，执行 `uv --version`，能看到版本号即可。若电脑没有 `winget`，使用上述官方页面中的其他安装方式。

在刚才解压的项目文件夹内，点击文件资源管理器的地址栏，输入 `powershell` 并回车。随后执行：

```powershell
uv sync --python 3.13
```

这里指定了本项目已测试的 Python 系列。uv 可按需下载 Python，无需先自行处理多个 Python 版本；具体行为见 [uv Python 安装说明](https://docs.astral.sh/uv/guides/install-python/)。依赖下载完成、终端重新出现输入提示后再继续。首次下载所需时间取决于网络。

## 3. 先体验示例，不用登录 BOSS

双击 `Start-Job-Radar.cmd`，或在项目终端运行：

```powershell
uv run python -m job_radar ui
```

看到本地工作台后，点 **“体验合成示例”**，检查能否看到岗位卡片、JD、筛选原因和导出文件。示例不请求 BOSS，不需要安装扩展、提供简历或填写模型 API Key。请保持启动工作台的终端开着；关闭终端会停止网页服务。

## 4. 连接自己的 Chrome

在项目文件夹再开一个终端，运行：

```powershell
uv run python -m job_radar setup
```

1. 复制命令打印的扩展文件夹路径。
2. 在日常 Chrome 地址栏打开 `chrome://extensions`，开启右上角“开发者模式”。
3. 点“加载已解压的扩展程序”，选择刚才打印的文件夹（应包含 `manifest.json`）。
4. 在同一个 Chrome 正常登录 BOSS 直聘，确认能看到职位列表，再刷新 Job Radar 工作台。

“Chrome 插件已连接”只表示连接成功。实际登录或安全验证状态会在搜索时检查；需要验证时，回到网页人工处理。

## 5. 做一次小范围真实搜索

先填一个你真正考虑的岗位关键词、选一个城市，其他筛选暂时留空。建议从以下设置开始：

| 界面选项 | 第一次建议值 |
| --- | --- |
| JD 深度 | 初筛后补 JD |
| 缺失信息 | 保留，稍后人工判断 |
| 每组页数 | 1 |
| 采集调用数 | 4 |
| 补 JD 数 | 3 |
| 岗位数 | 5 |
| 秒数 | 180 |
| 导出格式 / 范围 | Excel / 全部岗位 |

点 **“搜索并采集”**。保持扩展打开的 BOSS 专用搜索页可见，避免手动改词或翻页。岗位数量是上限，不保证一定返回 5 条；这些预算也不会补齐所有岗位的 JD。筛选在采集后本地应用，不会把全部条件传给 BOSS 搜索页。

在结果里检查：标题/公司/链接是否对应，JD 是否标为已采集，筛选依据是否符合你的判断。达到上限而停止属于预期结果，不能把本轮结果当成平台所有岗位。

## 6. 把清单用于求职

把值得继续了解的岗位改成 **“考虑”**，在备注中写“为什么合适 / 还缺什么证据 / 下一步”。在“导出内容”中选择所需列，点“按当前设置重新导出”。Excel 链接和其他已选格式会出现在结果区。

打开原岗位页面核对后，由你在 BOSS 里投递或沟通；再手动将记录改成“已投”或“面试”。这些状态是本地台账，不会自动同步 BOSS。

## 卡住时怎么处理

| 你看到的情况 | 下一步 |
| --- | --- |
| `uv` 不是命令 / 双击后一闪而过 | 重新开终端运行 `uv --version`，确认 uv 已安装；从终端运行启动命令以查看报错 |
| 找不到 `pyproject.toml` | 切到包含该文件的项目目录，再执行命令 |
| 下载依赖失败 | 核对网络和终端错误；下载完成前工作台不能运行 |
| 浏览器没有自动打开 | 手动访问终端打印的本地地址，默认 `http://127.0.0.1:8766` |
| 端口被占用 | 执行 `uv run python -m job_radar ui --port 8767` |
| 扩展未连接 / 需要登录 | 按第 4 步检查扩展加载位置、同一个 Chrome 的登录状态，再刷新工作台 |
| 零条岗位 / JD 空白 | 先看任务状态与 JD 状态；参考[错误状态说明](job-radar-guide.md#搜索没有拿到岗位时)，不要只凭“有导出文件”判断成功 |

仍无法使用时，在 [Issues](https://github.com/HSLMLJL/boss-job-search/issues) 写下系统、Chrome 版本、操作步骤与脱敏报错。不要附账号凭据、个人简历或完整求职数据目录。
