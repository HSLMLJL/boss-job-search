Job Radar｜BOSS 直聘求职工具（Windows 64 位便携版）
================================================

首次使用：
1. 对 ZIP 右键选择“全部解压缩”，不要在压缩包预览窗口内启动。
2. 进入解压文件夹，双击 Start-Job-Radar.cmd，浏览器会自动打开工作台。
3. 点击“先体验示例”，不需要登录 BOSS，也不需要安装 Python 或 uv。
4. 真实搜索前，在工作台展开“连接 BOSS”说明：用日常 Chrome 打开
   chrome://extensions，开启开发者模式，点“加载已解压的扩展程序”，
   选择本文件旁边的 extension 文件夹。扩展显示名为 BOSS Agent Bridge。
5. 在同一个 Chrome 正常登录 BOSS，回到工作台刷新连接。
6. 填关键词、城市、筛选条件和采集预算，选是否补完整 JD，再开始搜索。
   保持 BOSS 专用搜索页可见；需要登录/验证时按网页提示手动完成。
7. 在工作台下载 Excel/CSV/JSON，记录状态和备注；点“退出应用”保存并退出。
   仅关闭网页不会结束本地应用。下次双击同一个启动脚本即可继续查看记录。

数据与升级：
- 任务、备注和导出在 %LOCALAPPDATA%\Job-Radar，更新包不会自动删除它们。
- 请保留解压文件夹；Chrome 扩展仍需要其中的 extension 文件。
- 重复双击会打开已运行的工作台；默认端口被占用时自动选择空闲端口。
- 无需管理员权限，不修改 PATH，不要求模型 API Key。搜索不会自动投递简历。
- 未启动时先确认 runtime 和 app 文件夹已完整解压；本地错误日志位于
  %LOCALAPPDATA%\Job-Radar\desktop.log。
- 原源码方式的数据仍在 ~/.boss-agent，本版不会自动迁移或覆盖。

图文步骤：https://github.com/HSLMLJL/boss-job-search/blob/main/docs/job-radar-quickstart.md
问题反馈：https://github.com/HSLMLJL/boss-job-search/issues
反馈前请去除账号凭据、简历、岗位数据等个人信息。

English quickstart
==================

1. Extract the ZIP to a folder you can write to. Do not run it inside the ZIP.
2. Double-click Start-Job-Radar.cmd. No Python, uv, administrator permission,
   or installer is required.
3. The first time you want to search BOSS, open Chrome's chrome://extensions,
   enable Developer mode, and choose "Load unpacked". Select this package's
   extension folder. Then sign in to BOSS normally in that Chrome profile.

The local workspace opens in your normal browser. It stores your Job Radar
tasks and exports under %LOCALAPPDATA%\Job-Radar, not next to this package.
The extension only connects to the local Job Radar Bridge. The product stops
when BOSS requests a login or security check; complete that check in Chrome and
then continue from the saved task.

This package is for 64-bit Windows and carries its own official CPython runtime.
It does not modify PATH, install software, or include browser cookies, job data,
or your previous local records.

For provenance, see PORTABLE-MANIFEST.json in the extracted folder and
THIRD_PARTY_NOTICES.md for licenses.
