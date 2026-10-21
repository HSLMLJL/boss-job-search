# Job Radar｜BOSS Zhipin Job Search Assistant: Search, Filters, JD Export & Application Tracking

[中文说明](README.md) · [GitHub repository](https://github.com/HSLMLJL/boss-job-search) · [Windows download](https://github.com/HSLMLJL/boss-job-search/releases/tag/v0.2.0)

Job Radar is a local workspace for BOSS Zhipin job seekers. Search by keywords and cities, apply local screening rules, optionally collect JDs for screened-in jobs, export an Excel/CSV/JSON application list, and record application progress.

The workflow is **search → screen → JD → application list → prepare materials manually → follow up**. Collected JDs are input for your own resume preparation, not automatic resume rewriting. Job Radar does not include resume generation, AI fit scoring, automatic applications, or automatic messages.

> Use your own authorized account and follow platform rules. The project does not promise complete platform coverage.

![Job Radar local workspace with clearly marked synthetic demo jobs](docs/images/job-radar-workspace.png)

*A small Windows normal-Chrome v0.1.0 Beta check saved five jobs from one page, collected three non-empty JDs, and exported three formats. This is not a platform-wide coverage claim.*

## Start with a safe demo

The Windows x64 portable edition includes its Python runtime and dependencies. No Python, uv, terminal setup, or AI key is required for normal UI use. You can [preview a synthetic output CSV](examples/job-radar-sample.csv) before downloading anything.

1. Download [Job-Radar-0.2.0-Windows-x64.zip](https://github.com/HSLMLJL/boss-job-search/releases/download/v0.2.0/Job-Radar-0.2.0-Windows-x64.zip), choose **Extract all**, and keep the extracted folder. The automatically generated “Source code” ZIP is for developers.
2. Double-click `Start-Job-Radar.cmd` in the extracted folder. The local workspace opens in your browser.
3. Choose **先体验示例** for synthetic jobs. No BOSS login or extension is needed for this step.
4. For real searches, follow **连接 BOSS** in the UI. In Chrome open `chrome://extensions`, enable Developer mode, choose **Load unpacked**, and select the displayed `extension` folder. Log in to BOSS in the same Chrome and refresh the connection in Job Radar.

Chrome extension loading and login are manual first-use steps. Application data stays in `%LOCALAPPDATA%\Job-Radar`; closing the browser tab does not stop the local app—use **退出应用**. Other operating systems have not received equivalent validation. For development, download the [source ZIP](https://github.com/HSLMLJL/boss-job-search/archive/refs/heads/main.zip), install [uv](https://docs.astral.sh/uv/), then run `uv sync --python 3.13` and `uv run python -m job_radar ui`. See the [Windows quickstart](docs/job-radar-quickstart.md) and [AI/CLI usage](docs/job-radar-agent-usage.md).

## What it does

| Need | Result |
| --- | --- |
| Find work in several cities | Merged keyword/city searches with source keywords, basic facts, and links |
| Prepare materials against real JDs | Exported screened-job JDs and requirements for manual use; see the [resume-preparation workflow](docs/job-radar-resume-workflow.md) |
| Track applications | Local status and notes that persist when the same job appears again |

`matched` JD collection means a job passed your **local rules**; it is not AI scoring. Filters are applied locally after list/JD collection, so not every rule is sent to the platform. `pages` is per keyword-city pair; requests, JD fetches, jobs, and seconds are shared run limits. Empty/unsupported detail panels, a budget limit, or interruption remain incomplete.

Keep the dedicated Chrome search tab visible during a run and do not change its search or page manually. A browser or extension restart keeps saved results but loses the live checkpoint; start a new search rather than expecting cross-restart resume.

Daily use is through the UI; JSON is optional for reusable configurations. The [usage guide](docs/job-radar-guide.md) covers filters, budgets, exports, and recovery. Core search, filters, tracking, and export do not need an LLM. Built on the MIT-licensed [boss-agent-cli](https://github.com/can4hou6joeng4/boss-agent-cli), with original notices retained. [MIT License](LICENSE).
