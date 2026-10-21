"""Optional Artifact Tool presentation adapter; no browser or database writes."""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Mapping

from boss_agent_cli.radar.delivery_workbook import build_delivery_workbook_view
from boss_agent_cli.radar.xlsx_links import patch_workbook_links


class DeliveryXlsxError(ValueError):
    """The configured offline presentation runtime could not export the bundle."""


def export_delivery_xlsx(
    bundle: Mapping[str, Any], *, output_path: Path, node_path: Path,
    artifact_modules: Path, renderer_path: Path, preview_dir: Path | None = None,
) -> dict[str, Any]:
    """Create a new workbook exclusively, after all source validation passes.

    This optional adapter does not vendor, install or license Artifact Tool.
    Runtime paths are explicit and no shell or network command is used.
    """
    view = build_delivery_workbook_view(bundle)
    output = Path(output_path).resolve()
    if output.suffix.lower() != ".xlsx":
        raise DeliveryXlsxError("output must have .xlsx suffix")
    if output.exists():
        raise DeliveryXlsxError(f"refusing to overwrite existing workbook: {output}")
    for name, path in (("Node", node_path), ("renderer", renderer_path)):
        if not Path(path).is_file():
            raise DeliveryXlsxError(f"{name} not found: {path}")
    if not Path(artifact_modules).is_dir():
        raise DeliveryXlsxError("Artifact Tool module root is missing")
    if preview_dir is not None:
        for name in ("jobs-head.png", "jobs-links.png", "summary.png"):
            if (Path(preview_dir) / name).exists():
                raise DeliveryXlsxError(f"refusing to overwrite existing preview: {name}")
    output.parent.mkdir(parents=True, exist_ok=True)
    # Only this invocation's isolated staging files are disposable.
    with tempfile.TemporaryDirectory(prefix=".radar-xlsx-", dir=output.parent) as staging:
        staging_path = Path(staging)
        view_path = staging_path / "view.json"
        temp_xlsx = staging_path / "delivery.xlsx"
        view_path.write_text(json.dumps(view, ensure_ascii=False), encoding="utf-8")
        command = [str(Path(node_path).resolve()), str(Path(renderer_path).resolve()),
                   str(view_path), str(temp_xlsx), str(Path(artifact_modules).resolve())]
        if preview_dir is not None:
            command.append(str(Path(preview_dir).resolve()))
        try:
            result = subprocess.run(command, check=True, capture_output=True, text=True,
                                    encoding="utf-8", timeout=300,
                                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            report = json.loads(result.stdout.strip().splitlines()[-1])
            links = [{"ref": f"L{index + 6}", "url": row["岗位链接"]}
                     for index, row in enumerate(view["sheets"]["本次扫描"]["rows"])]
            patch_workbook_links(temp_xlsx, links)
            # Same-volume exclusive hard link: an existing user file is never replaced.
            os.link(temp_xlsx, output)
        except (OSError, ValueError, IndexError, subprocess.SubprocessError) as exc:
            detail = getattr(exc, "stderr", None) or str(exc)
            raise DeliveryXlsxError(f"offline XLSX export failed: {str(detail)[-2000:]}") from exc
    return {"xlsx": str(output), "jobs": len(links), "renderer": report}
