# Job Radar｜BOSS Zhipin Job Search & Career Assistant

[中文说明](README.md)

**Get it:** [GitHub repository](https://github.com/HSLMLJL/boss-job-search) · [v0.1.0 Beta download page](https://github.com/HSLMLJL/boss-job-search/releases/tag/v0.1.0)

![Job Radar local workspace with clearly marked synthetic demo jobs](docs/images/job-radar-workspace.png)

Job Radar is an open-source tool that helps you find work on BOSS Zhipin. Search multiple keywords and cities, filter job listings, optionally collect full job descriptions, and export a follow-up list to Excel, CSV, or JSON.

Configure what to search, how much information to collect, and what to export; keep the results, optional full job descriptions, decisions, and follow-up status on your own computer.

It is designed for ordinary job seekers across industries and roles. AI Product Manager is one possible saved search, not the product boundary.

> Current source: BOSS Zhipin. Follow the platform rules and use only your own authorized account. Job Radar does not guarantee complete platform coverage and does not automatically apply or message on your behalf.


## What you can configure

| Area | Options |
| --- | --- |
| Search | Multiple keywords and cities; salary, experience, education, industry, company size/stage, job type, welfare, include/exclude terms |
| JD collection | `none`, `matched` (only filtered-in jobs), or `all` |
| Budget | Pages per keyword-city pair; task-wide requests, JD fetches, jobs, and wall-clock seconds |
| Export | Excel, CSV, JSON; selected columns; matched, all, or newly recorded jobs |
| Follow-up | Status and notes retained across later sightings of the same job |

Job Radar records *first seen locally* separately from a platform publication time. A stopped or capped run is marked as incomplete; it does not mean that no more jobs exist.

`limits.pages` is the maximum page count for each keyword-city pair. `requests`, `details`, `jobs`, and `seconds` are shared limits for each start or resume window. A shared limit may stop the task before later pairs are visited.

## Open the local workspace first

For ordinary use, you need Python 3.10+, [uv](https://docs.astral.sh/uv/), and your everyday Chrome browser. Open the [v0.1.0 Beta download page](https://github.com/HSLMLJL/boss-job-search/releases/tag/v0.1.0), download **Source code (ZIP)**, extract it, and run the following in the extracted project directory:

```bash
uv sync
```

For the first setup, start the local Browser Bridge:

```bash
uv run python -m job_radar setup
```

The command prints the absolute path of this repository's `extension` folder. Open `chrome://extensions`, turn on Developer mode, choose **Load unpacked**, and select that folder. Then sign in to BOSS Zhipin in the same everyday Chrome. This is a one-time local extension authorization: Chrome keeps the login session, so no Cookie copy or `boss login` command is needed.

On Windows, double-click `Start-Job-Radar.cmd` to start the local workspace. Or run:

```bash
uv run python -m job_radar ui
```

It opens `http://127.0.0.1:8766` on your computer and starts the local Bridge. The installed `job-radar` short command also works; if a Windows policy blocks that generated entry point, use the module commands shown here.

## Then try the CLI and synthetic demo

```bash
uv run python -m job_radar demo
```

`demo` uses synthetic data only. It does not log in, access BOSS Zhipin, require Chrome, or use an AI key. For a real search, complete the extension setup above and sign in through everyday Chrome, then run:

```bash
uv run python -m job_radar init --output my-search.json
uv run python -m job_radar run my-search.json
```

Continue or export a saved run:

```bash
uv run python -m job_radar resume RUN_ID
uv run python -m job_radar export RUN_ID
```

See the [Chinese usage guide](docs/job-radar-guide.md), [general example](examples/job-radar-general.json), and [operations example](examples/job-radar-operations.json). Default data lives in `~/.boss-agent/job-radar`; use `--data-dir PATH` to isolate another local workspace. It does not move or copy Chrome's login session.

## Privacy and scope

The local workspace stores task configurations, job history, statuses, and notes. Core collection, filtering, tracking, and export do not require an LLM. AI analysis, the existing CLI, and MCP are advanced capabilities.

Real search uses a dedicated Chrome search tab bound by the extension. Keep that tab visible while a run is active; do not manually change its query or page at the same time. Filtering is applied locally after list/JD capture, so not every filter is necessarily sent as a platform search parameter. A full JD requires a visible, verified job-detail panel; empty or unsupported panels remain incomplete. Saved results persist, but a browser or extension restart loses the live page checkpoint and requires a new search rather than a cross-restart resume.

## Acknowledgements

Built on the MIT-licensed [boss-agent-cli](https://github.com/can4hou6joeng4/boss-agent-cli), with original notices and license retained.

## License

[MIT License](LICENSE)
