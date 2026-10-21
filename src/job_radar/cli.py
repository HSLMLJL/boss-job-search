"""Friendly local UI plus scriptable commands using the same task contract."""

from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import threading
import webbrowser

import click

from job_radar import __version__
from job_radar.config import DEFAULT_CONFIG, TaskConfig
from job_radar.exporter import export_run
from job_radar.service import auth_status, execute_run
from job_radar.store import JobStore


@click.group(invoke_without_command=True)
@click.option("--data-dir", type=click.Path(path_type=Path), default="~/.boss-agent", help="本地任务和岗位数据目录")
@click.version_option(__version__, prog_name="Job Radar")
@click.pass_context
def cli(ctx: click.Context, data_dir: Path):
	"""Job Radar｜BOSS 直聘职位搜索与求职助手。直接运行打开本地工作台。"""
	ctx.ensure_object(dict)
	ctx.obj["data_dir"] = data_dir.expanduser().resolve()
	if ctx.invoked_subcommand is None:
		ctx.invoke(ui_cmd)


@cli.command("ui")
@click.option("--port", type=click.IntRange(1024, 65535), default=8766, show_default=True)
@click.option("--open/--no-open", "open_browser", default=True, help="自动打开本地界面")
@click.pass_context
def ui_cmd(ctx, port=8766, open_browser=True):
	"""打开任务配置、岗位清单和导出界面。"""
	from aiohttp import web
	from job_radar.server import create_app

	try:
		_start_bridge()
	except Exception:
		click.echo(
			"提示：本地 Chrome Bridge 未能启动；合成示例和本地记录仍可使用。"
			"真实 BOSS 搜索请先运行 setup 或检查 Bridge。",
			err=True,
		)
	address = f"http://127.0.0.1:{port}"

	def started(_message):
		click.echo(f"Job Radar｜BOSS 直聘职位搜索与求职助手\n本地工作台：{address}\n按 Ctrl+C 停止服务。")
		if open_browser:
			webbrowser.open(address)

	try:
		web.run_app(create_app(ctx.obj["data_dir"]), host="127.0.0.1", port=port, print=started)
	except OSError as exc:
		raise click.ClickException(
			f"工作台未能启动，端口 {port} 可能已被占用。请用 ui --port {port + 1} 更换端口。"
		) from exc


def _start_bridge():
	from boss_agent_cli.bridge.client import BridgeClient
	from boss_agent_cli.bridge.daemon import is_daemon_running, start_daemon_background

	client = BridgeClient()
	if client.is_running() and client.status() is not None:
		return
	if not is_daemon_running():
		start_daemon_background()
	if not client.is_running() or client.status() is None:
		raise RuntimeError("本地 Chrome Bridge 未就绪，请检查 19826 端口是否被其他程序占用")


def _require_bridge():
	try:
		_start_bridge()
	except Exception as exc:
		raise click.ClickException("无法启动本地 Chrome Bridge。请运行 setup 并检查 Chrome 扩展后重试。") from exc


@cli.command("setup")
@click.pass_context
def setup_cmd(ctx):
	"""启动本地连接服务，并显示 Chrome 扩展安装步骤。"""
	from job_radar.onboarding import install_extension_path

	_require_bridge()
	click.echo("1. 在 Chrome 打开 chrome://extensions，开启开发者模式。")
	click.echo("2. 点击「加载已解压的扩展程序」，选择以下目录：")
	click.echo(str(install_extension_path()))
	click.echo("3. 在这个 Chrome 中正常登录 BOSS，再打开 Job Radar 工作台。")
	click.echo(auth_status(ctx.obj["data_dir"])["message"])


@cli.command("init")
@click.option("--output", type=click.Path(path_type=Path), default="my-search.json")
def init_cmd(output):
	"""生成可编辑的任务配置，不采集岗位。"""
	if output.exists():
		raise click.ClickException("文件已存在，请更换输出名称")
	output.write_text(json.dumps(DEFAULT_CONFIG, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
	click.echo(str(output.resolve()))


def _execute(ctx, config, demo=False, run_id=None):
	store = JobStore(ctx.obj["data_dir"])
	if not demo:
		_require_bridge()
	if not demo and not auth_status(ctx.obj["data_dir"])["ready"]:
		raise click.ClickException(auth_status(ctx.obj["data_dir"])["message"])
	run = store.get_run(run_id) if run_id else store.create_run(config.to_dict(), demo=demo)
	if not run:
		raise click.ClickException("未找到运行记录")
	if run.get("status") == "completed":
		raise click.ClickException("任务已完成；重新采集请使用 run 创建新任务")
	stop = threading.Event()
	try:
		outcome = execute_run(store, run["id"], stop_event=stop)
	except KeyboardInterrupt:
		stop.set()
		store.update_run(run["id"], status="stopped", message="用户中断，已保存进度")
		export_run(store, run["id"])
		outcome = store.get_run(run["id"])
	except (ValueError, RuntimeError) as exc:
		raise click.ClickException(str(exc)) from exc
	click.echo(
		json.dumps({key: value for key, value in outcome.items() if key != "checkpoint"}, ensure_ascii=False, indent=2)
	)
	click.echo(f"结果目录：{store.root / 'exports' / run['id']}")
	if outcome.get("status") in {"failed", "auth_required", "risk_stopped"}:
		ctx.exit(1)


@cli.command("run")
@click.argument("config_file", type=click.Path(exists=True, path_type=Path))
@click.option("--demo", is_flag=True, help="使用合成示例数据，不连接 BOSS")
@click.pass_context
def run_cmd(ctx, config_file, demo):
	"""执行 JSON 任务配置。"""
	try:
		config = TaskConfig.from_dict(json.loads(config_file.read_text(encoding="utf-8-sig")))
	except ValueError as exc:
		raise click.ClickException(str(exc)) from exc
	_execute(ctx, config, demo=demo)


@cli.command("demo")
@click.pass_context
def demo_cmd(ctx):
	"""用明确标记的合成岗位体验完整流程，无需登录或模型密钥。"""
	config = deepcopy(DEFAULT_CONFIG)
	config["name"] = "合成示例 · 求职流程体验"
	config["export"]["formats"] = ["xlsx", "csv", "json"]
	_execute(ctx, TaskConfig.from_dict(config), demo=True)


@cli.command("resume")
@click.argument("run_id")
@click.pass_context
def resume_cmd(ctx, run_id):
	"""从已保存的任务进度继续；本次继续使用新的预算窗口。"""
	run = JobStore(ctx.obj["data_dir"]).get_run(run_id)
	if not run:
		raise click.ClickException("未找到运行记录")
	_execute(ctx, TaskConfig.from_dict(run["config"]), demo=bool(run.get("demo")), run_id=run_id)


@cli.command("export")
@click.argument("run_id")
@click.pass_context
def export_cmd(ctx, run_id):
	"""使用保存的字段和格式，重新导出最新人工状态与备注。"""
	store = JobStore(ctx.obj["data_dir"])
	try:
		exports = export_run(store, run_id)
	except KeyError as exc:
		raise click.ClickException("未找到运行记录") from exc
	for item in exports:
		click.echo(str(store.root / "exports" / item["relative"]))


if __name__ == "__main__":
	cli()
