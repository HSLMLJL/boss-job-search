"""Native OOXML external-hyperlink metadata patching for caller-owned workbooks.

Cell values and styles are deliberately outside this module's scope.  It only
replaces the selected worksheet's hyperlink XML and matching relationship rows.
"""
from __future__ import annotations

import os
import posixpath
import re
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Mapping, Sequence
from zipfile import ZIP_DEFLATED, ZipFile

_SHEET_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
_REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_PACKAGE_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
_HYPERLINK_REL_TYPE = f"{_REL_NS}/hyperlink"
_CELL_REF = re.compile(r"^\$?([A-Z]{1,3})\$?([1-9][0-9]*)$")
# ``~`` is an RFC 3986 unreserved character and is emitted in real BOSS
# encrypted job IDs.  Keep this allow-list narrow while accepting that valid
# canonical path character.
_BOSS_URL = re.compile(r"^https://www\.zhipin\.com/job_detail/[A-Za-z0-9_~-]+\.html$")
_MAX_COLUMN = 16384  # XFD
_MAX_ROW = 1048576

ET.register_namespace("", _SHEET_NS)
ET.register_namespace("r", _REL_NS)


def patch_workbook_links(
    path: Path,
    links: list[dict[str, str]],
    sheet_name: str = "本次扫描",
) -> None:
    """Replace a worksheet's native external BOSS-job hyperlinks atomically.

    ``path`` must be a caller-owned temporary ``.xlsx`` file.  The function
    validates all link targets before reading or replacing any ZIP member and
    verifies the exact links from the temporary workbook before the final rename.
    """
    workbook_path = Path(path)
    normalized = _validate_links(links)
    if workbook_path.suffix.lower() != ".xlsx" or not workbook_path.is_file():
        raise ValueError(f"workbook must be an existing .xlsx file: {workbook_path}")

    with ZipFile(workbook_path) as source:
        sheet_part = _sheet_part(source, sheet_name)
        rel_part = _relationship_part(sheet_part)
        sheet_root = ET.fromstring(source.read(sheet_part))
        rel_root = (
            ET.fromstring(source.read(rel_part))
            if rel_part in source.namelist()
            else ET.Element(f"{{{_PACKAGE_REL_NS}}}Relationships")
        )
        _replace_hyperlink_nodes(sheet_root, rel_root, normalized)
        temp_name = _write_temporary_workbook(source, workbook_path, sheet_part, rel_part, sheet_root, rel_root, normalized)
    try:
        if read_workbook_links(temp_name, sheet_name=sheet_name) != normalized:
            raise ValueError("native hyperlink readback did not match requested links")
        os.replace(temp_name, workbook_path)
    except BaseException:
        Path(temp_name).unlink(missing_ok=True)
        raise


def read_workbook_links(path: Path, sheet_name: str = "本次扫描") -> list[dict[str, str]]:
    """Read a worksheet's native external hyperlink metadata without mutation."""
    with ZipFile(path) as source:
        sheet_part = _sheet_part(source, sheet_name)
        rel_part = _relationship_part(sheet_part)
        root = ET.fromstring(source.read(sheet_part))
        hyperlinks = root.find(f"{{{_SHEET_NS}}}hyperlinks")
        if hyperlinks is None:
            return []
        if rel_part not in source.namelist():
            raise ValueError("worksheet contains hyperlinks but has no relationships part")
        relationships = ET.fromstring(source.read(rel_part))
        targets = {
            item.attrib.get("Id", ""): item.attrib.get("Target", "")
            for item in relationships
            if item.attrib.get("Type") == _HYPERLINK_REL_TYPE and item.attrib.get("TargetMode") == "External"
        }
        result: list[dict[str, str]] = []
        for item in hyperlinks:
            rel_id = item.attrib.get(f"{{{_REL_NS}}}id", "")
            if rel_id not in targets:
                raise ValueError("worksheet hyperlink references a missing external relationship")
            result.append({"ref": item.attrib.get("ref", ""), "url": targets[rel_id]})
        return result


def _validate_links(links: Sequence[Mapping[str, Any]]) -> list[dict[str, str]]:
    if not isinstance(links, list):
        raise ValueError("links must be a list")
    result: list[dict[str, str]] = []
    refs: set[str] = set()
    for index, item in enumerate(links, 1):
        if not isinstance(item, Mapping):
            raise ValueError(f"links[{index}] must be an object")
        ref, url = item.get("ref"), item.get("url")
        if not isinstance(ref, str) or not _valid_ref(ref):
            raise ValueError(f"links[{index}].ref must be one valid worksheet cell reference")
        if ref in refs:
            raise ValueError(f"duplicate link cell reference: {ref}")
        if not isinstance(url, str) or not _BOSS_URL.fullmatch(url):
            raise ValueError(f"links[{index}].url must be a canonical BOSS job-detail HTTPS URL")
        refs.add(ref)
        result.append({"ref": ref, "url": url})
    return result


def _valid_ref(ref: str) -> bool:
    match = _CELL_REF.fullmatch(ref)
    if match is None:
        return False
    letters, row = match.groups()
    column = 0
    for letter in letters:
        column = column * 26 + ord(letter) - ord("A") + 1
    return column <= _MAX_COLUMN and int(row) <= _MAX_ROW


def _sheet_part(source: ZipFile, sheet_name: str) -> str:
    workbook = ET.fromstring(source.read("xl/workbook.xml"))
    relationships = ET.fromstring(source.read("xl/_rels/workbook.xml.rels"))
    targets = {item.attrib.get("Id", ""): item.attrib.get("Target", "") for item in relationships}
    sheets = workbook.find(f"{{{_SHEET_NS}}}sheets")
    if sheets is None:
        raise ValueError("workbook has no sheets")
    sheet = next((item for item in sheets if item.attrib.get("name") == sheet_name), None)
    if sheet is None:
        raise ValueError(f"worksheet not found: {sheet_name}")
    target = targets.get(sheet.attrib.get(f"{{{_REL_NS}}}id", ""))
    if not target:
        raise ValueError(f"worksheet relationship not found: {sheet_name}")
    normalized_target = target.lstrip("/")
    if normalized_target.startswith("xl/"):
        return posixpath.normpath(normalized_target)
    return posixpath.normpath(posixpath.join("xl", normalized_target))


def _relationship_part(sheet_part: str) -> str:
    parent, filename = posixpath.split(sheet_part)
    return f"{parent}/_rels/{filename}.rels"


def _replace_hyperlink_nodes(
    sheet_root: ET.Element,
    rel_root: ET.Element,
    links: list[dict[str, str]],
) -> None:
    for item in list(rel_root):
        if item.attrib.get("Type") == _HYPERLINK_REL_TYPE:
            rel_root.remove(item)
    for item in list(sheet_root):
        if item.tag == f"{{{_SHEET_NS}}}hyperlinks":
            sheet_root.remove(item)
    if not links:
        return
    hyperlinks = ET.Element(f"{{{_SHEET_NS}}}hyperlinks")
    _insert_hyperlinks_in_schema_order(sheet_root, hyperlinks)
    used_ids = {item.attrib.get("Id", "") for item in rel_root}
    next_id = 1
    for link in links:
        while f"rId{next_id}" in used_ids:
            next_id += 1
        rel_id = f"rId{next_id}"
        used_ids.add(rel_id)
        next_id += 1
        ET.SubElement(
            rel_root,
            f"{{{_PACKAGE_REL_NS}}}Relationship",
            {"Id": rel_id, "Type": _HYPERLINK_REL_TYPE, "Target": link["url"], "TargetMode": "External"},
        )
        ET.SubElement(hyperlinks, f"{{{_SHEET_NS}}}hyperlink", {"ref": link["ref"], f"{{{_REL_NS}}}id": rel_id})


def _insert_hyperlinks_in_schema_order(sheet_root: ET.Element, hyperlinks: ET.Element) -> None:
    # hyperlinks follows dataValidations and precedes print/layout/drawing nodes.
    after = {"sheetData", "sheetCalcPr", "sheetProtection", "protectedRanges", "scenarios", "autoFilter", "sortState", "dataConsolidate", "customSheetViews", "mergeCells", "phoneticPr", "conditionalFormatting", "dataValidations"}
    last_after = -1
    for index, child in enumerate(sheet_root):
        local_name = child.tag.rsplit("}", 1)[-1]
        if local_name in after:
            last_after = index
        elif last_after >= 0:
            sheet_root.insert(index, hyperlinks)
            return
    sheet_root.insert(last_after + 1 if last_after >= 0 else len(sheet_root), hyperlinks)


def _write_temporary_workbook(
    source: ZipFile,
    workbook_path: Path,
    sheet_part: str,
    rel_part: str,
    sheet_root: ET.Element,
    rel_root: ET.Element,
    links: list[dict[str, str]],
) -> str:
    descriptor, temp_name = tempfile.mkstemp(prefix="xlsx-links-", suffix=".xlsx", dir=workbook_path.parent)
    os.close(descriptor)
    try:
        with ZipFile(temp_name, "w", ZIP_DEFLATED) as output:
            for item in source.infolist():
                if item.filename not in {sheet_part, rel_part}:
                    output.writestr(item, source.read(item.filename))
            output.writestr(sheet_part, ET.tostring(sheet_root, encoding="utf-8", xml_declaration=True))
            if links or rel_part in source.namelist():
                output.writestr(rel_part, ET.tostring(rel_root, encoding="utf-8", xml_declaration=True))
    except BaseException:
        Path(temp_name).unlink(missing_ok=True)
        raise
    return temp_name
