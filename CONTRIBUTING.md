# 参与开发

先阅读 [README](README.md) 和 [配置指南](docs/job-radar-guide.md)。

```bash
uv sync --extra dev
uv run python -m pytest
uv run python -m ruff check src/job_radar tests/test_job_radar_*.py
uv run python -m job_radar demo
```

默认测试集覆盖 Job Radar 产品及复用的搜索筛选模块，使用合成数据，不访问招聘平台。请为采集、预算、断点、筛选、去重或导出行为的修改提供相应离线回归测试。真实账号验证由操作者主动发起，不能并入无人值守 CI。

提交修改时说明用户遇到的问题、最终行为与验证方式。不要提交本地数据库、登录信息、个人求职材料或未脱敏的运行输出。请保留原有版权与许可声明。
