"""Loopback-only local workspace. No public hosting or remote service is required."""

from __future__ import annotations

import asyncio
from copy import deepcopy
from pathlib import Path
import secrets
from urllib.parse import quote

from aiohttp import web

from job_radar.config import DEFAULT_CONFIG, TaskConfig, options
from job_radar.exporter import export_run, public_job
from job_radar.service import Workspace, auth_status

WEB_ROOT = Path(__file__).parent / "web"
WORKSPACE_KEY = web.AppKey("workspace", Workspace)


def visible_run(run: dict) -> dict:
    return {key: val for key, val in run.items() if key not in {"checkpoint", "exports"}}


def create_app(data_dir: Path, *, workspace: Workspace | None = None) -> web.Application:
    workspace = workspace or Workspace(data_dir)
    token = secrets.token_urlsafe(32)

    @web.middleware
    async def local_only(request, handler):
        host = request.host.split(":", 1)[0].lower()
        if host not in {"localhost", "127.0.0.1"}:
            return web.json_response({"error": "仅允许本机访问"}, status=403)
        origin = request.headers.get("Origin")
        if origin and origin != f"{request.scheme}://{request.host}":
            return web.json_response({"error": "拒绝跨站请求"}, status=403)
        if request.method not in {"GET", "HEAD"}:
            if not secrets.compare_digest(request.headers.get("X-Job-Radar-Token", ""), token):
                return web.json_response({"error": "页面已过期，请刷新后重试"}, status=403)
            if request.content_type != "application/json":
                return web.json_response({"error": "需要 JSON 请求"}, status=415)
        try:
            response = await handler(request)
        except KeyError:
            response = web.json_response({"error": "未找到此任务或岗位"}, status=404)
        except (ValueError, TypeError) as exc:
            response = web.json_response({"error": str(exc)}, status=400)
        except web.HTTPException:
            raise
        except Exception:
            response = web.json_response({"error": "操作未完成，请刷新页面或检查本地日志"}, status=500)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Cache-Control"] = "no-store"
        response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
        return response

    app = web.Application(middlewares=[local_only], client_max_size=512 * 1024)
    app[WORKSPACE_KEY] = workspace

    async def index(request):
        return web.FileResponse(WEB_ROOT / "index.html")

    async def static(request):
        name = request.match_info["name"]
        if name not in {"app.js", "style.css"}:
            raise web.HTTPNotFound()
        return web.FileResponse(WEB_ROOT / name)

    async def bootstrap(request):
        return web.json_response({"defaults": deepcopy(DEFAULT_CONFIG), "options": options(),
            "templates": workspace.store.list_templates(), "runs": [visible_run(run) for run in workspace.store.list_runs()],
            "auth": auth_status(data_dir), "csrf_token": token})

    async def templates(request):
        body = await request.json()
        config = TaskConfig.from_dict(body.get("config"))
        return web.json_response(workspace.store.save_template(config.to_dict(), body.get("id")))

    async def runs(request):
        if request.method == "GET":
            return web.json_response({"runs": [visible_run(run) for run in workspace.store.list_runs()]})
        body = await request.json()
        if not isinstance(body.get("demo", False), bool):
            raise ValueError("demo 需要为开关值")
        config = TaskConfig.from_dict(body.get("config"))
        demo = body.get("demo", False)
        if not demo and not auth_status(data_dir)["ready"]:
            return web.json_response({"error": auth_status(data_dir)["message"]}, status=409)
        run = workspace.start(config, demo=demo)
        return web.json_response({"id": run["id"]}, status=201)

    async def run_detail(request):
        run_id = request.match_info["run_id"]
        run = workspace.store.get_run(run_id)
        if not run:
            raise KeyError(run_id)
        exports = [{"name": item["name"], "url": "/downloads/" + quote(item["relative"], safe="/")} for item in run.get("exports", [])]
        # Checkpoint contains source identifiers; it is only needed by the runner.
        visible = visible_run(run)
        return web.json_response({"run": visible, "jobs": [public_job(job) for job in workspace.store.get_jobs(run_id)], "exports": exports})

    async def stop(request):
        return web.json_response(visible_run(workspace.stop(request.match_info["run_id"])))

    async def resume(request):
        run_id = request.match_info["run_id"]
        run = workspace.store.get_run(run_id)
        if not run:
            raise KeyError(run_id)
        if not run.get("demo") and not auth_status(data_dir)["ready"]:
            return web.json_response({"error": auth_status(data_dir)["message"]}, status=409)
        config = TaskConfig.from_dict(run["config"])
        result = workspace.start(config, run_id=run_id)
        return web.json_response({"id": result["id"]})

    async def export(request):
        run_id = request.match_info["run_id"]
        body = await request.json()
        run = workspace.store.get_run(run_id)
        if not run:
            raise KeyError(run_id)
        config = TaskConfig.from_dict(body.get("config", run["config"]))
        await asyncio.to_thread(export_run, workspace.store, run_id, config)
        return await run_detail(request)

    async def job_status(request):
        body = await request.json()
        workspace.store.update_job_status(request.match_info["job_key"], body.get("status"), body.get("notes", ""))
        return web.json_response({"ok": True})

    async def download(request):
        root = (workspace.store.root / "exports").resolve()
        path = (root / request.match_info["path"]).resolve()
        if not path.is_relative_to(root) or path.suffix not in {".xlsx", ".csv", ".json"} or not path.is_file():
            raise web.HTTPNotFound()
        response = web.FileResponse(path)
        response.headers["Content-Disposition"] = "attachment; filename*=UTF-8''" + quote(path.name)
        return response

    async def shutdown(app):
        workspace.shutdown()

    app.on_shutdown.append(shutdown)
    app.router.add_get("/", index)
    app.router.add_get("/static/{name}", static)
    app.router.add_get("/api/bootstrap", bootstrap)
    app.router.add_post("/api/templates", templates)
    app.router.add_get("/api/runs", runs)
    app.router.add_post("/api/runs", runs)
    app.router.add_get("/api/runs/{run_id}", run_detail)
    app.router.add_post("/api/runs/{run_id}/stop", stop)
    app.router.add_post("/api/runs/{run_id}/resume", resume)
    app.router.add_post("/api/runs/{run_id}/export", export)
    app.router.add_post("/api/jobs/{job_key}/status", job_status)
    app.router.add_get("/downloads/{path:.*}", download)
    return app
