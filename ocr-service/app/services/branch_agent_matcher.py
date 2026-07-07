"""
Auto-match mã chi nhánh / đại lý theo tên chi nhánh + phòng GD.
"""

from __future__ import annotations

import logging
import re
import unicodedata
from difflib import SequenceMatcher

from app.config import settings
from app.models.schemas import BranchAgentMatchResult, MatchStatus
from app.services.banca_core_service import (
    BancaCoreClient,
    flatten_descendants,
    get_client,
    parse_agency_item,
    parse_agent_enrichment,
)

logger = logging.getLogger(__name__)

_PREFIX_RE = re.compile(
    r"^(cn|chi nhanh|pgd|phong giao dich|phòng giao dịch)\s+",
    re.IGNORECASE,
)
_DEPT_CODE_RE = re.compile(r"^(\d{4})\s+(.+)$")


def _parse_department_for_enrich(dept_name: str) -> tuple[str, str]:
    """Parse '6900 Hội sở' -> (branch_code, branch_name)."""
    m = _DEPT_CODE_RE.match((dept_name or "").strip())
    if m:
        return m.group(1), m.group(2).strip()
    return "", ""


def normalize_vn(text: str) -> str:
    """Lowercase, bỏ dấu, gộp khoảng trắng, bỏ tiền tố CN/PGD."""
    s = str(text or "").strip().lower()
    s = s.replace("đ", "d").replace("Đ", "d")
    s = unicodedata.normalize("NFD", s)
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    s = " ".join(s.split())
    s = _PREFIX_RE.sub("", s)
    return s.strip()


def fuzzy_score(a: str, b: str) -> float:
    na, nb = normalize_vn(a), normalize_vn(b)
    if not na or not nb:
        return 0.0
    if na == nb:
        return 1.0
    if na in nb or nb in na:
        return 0.92
    return SequenceMatcher(None, na, nb).ratio()


def _classify_score(top: float, second: float) -> tuple[MatchStatus, float]:
    threshold = settings.banca_core_match_threshold
    suggest = settings.banca_core_match_suggest_threshold
    gap = settings.banca_core_match_min_gap
    if top >= threshold and (top - second) >= gap:
        return MatchStatus.AUTO, top
    if top >= suggest:
        return MatchStatus.SUGGEST, top
    return MatchStatus.MANUAL, top


def _pick_best(candidates: list[tuple[float, dict]]) -> tuple[dict | None, MatchStatus, float]:
    if not candidates:
        return None, MatchStatus.MANUAL, 0.0
    candidates.sort(key=lambda x: x[0], reverse=True)
    top_score = candidates[0][0]
    second_score = candidates[1][0] if len(candidates) > 1 else 0.0
    status, conf = _classify_score(top_score, second_score)
    return candidates[0][1], status, conf


def match_branch_and_department(
    client: BancaCoreClient,
    branch_name: str,
    department_name: str = "",
) -> BranchAgentMatchResult:
    """Tìm chi nhánh + PGD theo tên."""
    result = BranchAgentMatchResult()
    if not branch_name.strip():
        result.match_status = MatchStatus.MANUAL
        return result

    try:
        agencies = client.search_agencies(branch_name, size=30)
    except Exception as exc:
        logger.warning("search_agencies lỗi: %s", exc)
        result.match_status = MatchStatus.MANUAL
        result.warnings.append(str(exc))
        return result

    scored = [
        (fuzzy_score(branch_name, a.get("name", "")), parse_agency_item(a))
        for a in agencies
    ]
    best, status, conf = _pick_best(scored)
    if not best:
        result.match_status = MatchStatus.MANUAL
        return result

    result.branch_name_matched = best.get("name", "")
    result.branch_code = best.get("core_bank_code", "")
    result.agency_id = best.get("id", "")
    result.agent_code = best.get("agency_code", "")
    result.match_status = status
    result.match_confidence = conf

    if department_name.strip() and best.get("id"):
        try:
            tree = client.get_agency_descendants(best["id"])
            nodes = flatten_descendants(tree)
            pgd_scored = [
                (fuzzy_score(department_name, n.get("name", "")), n)
                for n in nodes
                if n.get("name")
            ]
            pgd_best, pgd_status, pgd_conf = _pick_best(pgd_scored)
            if pgd_best and pgd_conf >= settings.banca_core_match_suggest_threshold:
                result.department_name_matched = pgd_best.get("name", "")
                pgd_code = (pgd_best.get("coreBankCode") or "").strip()
                if pgd_code:
                    result.branch_code = pgd_code
                infos = pgd_best.get("agencyInfos") or []
                for info in infos:
                    code = (info.get("agencyCode") or "").strip()
                    if code:
                        result.agent_code = code
                        break
                if pgd_status == MatchStatus.AUTO or pgd_conf > conf:
                    result.match_status = pgd_status
                    result.match_confidence = pgd_conf
        except Exception as exc:
            result.warnings.append(f"descendants: {exc}")

    return result


def enrich_user_row(
    user_data: dict,
    client: BancaCoreClient | None = None,
) -> dict:
    """
    Enrich một dòng user: auto-match tên CN/PGD, fallback email.
    Trả dict cập nhật (không mutate input).
    """
    row = dict(user_data)
    warnings: list[str] = list(row.get("warnings") or [])

    if row.get("branch_code") and row.get("agent_code"):
        row["match_status"] = row.get("match_status") or MatchStatus.MANUAL.value
        return row

    cli = client or get_client()
    if cli is None:
        row["match_status"] = MatchStatus.MANUAL.value
        return row

    dept_name = row.get("department_name") or ""
    if dept_name and not row.get("branch_code"):
        parsed_code, parsed_name = _parse_department_for_enrich(dept_name)
        if parsed_code and not row.get("branch_code"):
            row["branch_code"] = parsed_code
        if parsed_name and not row.get("branch_name"):
            row["branch_name"] = parsed_name

    branch_name = row.get("branch_name") or ""
    if not branch_name and dept_name:
        branch_name = dept_name

    if branch_name and not row.get("branch_code"):
        match = match_branch_and_department(cli, branch_name, dept_name)
        warnings.extend(match.warnings)
        if match.branch_code:
            row["branch_code"] = match.branch_code
        if match.agent_code and not row.get("agent_code"):
            row["agent_code"] = match.agent_code
        if match.branch_name_matched:
            row["branch_name_matched"] = match.branch_name_matched
        if match.department_name_matched:
            row["department_name_matched"] = match.department_name_matched
        row["match_status"] = match.match_status.value
        row["match_confidence"] = match.match_confidence
    elif row.get("branch_code") and not row.get("agent_code"):
        try:
            agencies = cli.search_agencies(row["branch_code"], size=10)
            for a in agencies:
                parsed = parse_agency_item(a)
                code = (parsed.get("core_bank_code") or "").strip()
                if code == row["branch_code"]:
                    row["agent_code"] = parsed.get("agency_code", "") or row.get(
                        "agent_code", ""
                    )
                    row["branch_name_matched"] = parsed.get("name", "")
                    row["match_status"] = MatchStatus.AUTO.value
                    row["enrich_source"] = "branch_code"
                    break
        except Exception as exc:
            warnings.append(f"branch_code lookup: {exc}")

    email = (row.get("email") or "").strip()
    if email and (not row.get("branch_code") or not row.get("agent_code")):
        try:
            agent = cli.lookup_agent_by_email(email)
            if agent:
                parsed = parse_agent_enrichment(agent)
                if parsed["branch_code"] and not row.get("branch_code"):
                    row["branch_code"] = parsed["branch_code"]
                if parsed["agent_code"] and not row.get("agent_code"):
                    row["agent_code"] = parsed["agent_code"]
                if parsed["branch_name"] and not row.get("branch_name_matched"):
                    row["branch_name_matched"] = parsed["branch_name"]
                row["match_status"] = MatchStatus.AUTO.value
                row["enrich_source"] = "email"
        except Exception as exc:
            warnings.append(f"email lookup: {exc}")

    row["warnings"] = warnings
    return row
