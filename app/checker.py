"""套件 / 器件双向核对。

判定口径：
- 缺记录：套件列出了某器件编号，但完整抓取后目录中不存在该编号的器件详情。
- 归属不符：两份均已成功抓取的页面直接证实的互指不一致（含所属套件未收录且
  套件目录完整、套件未列入该器件、器件未注明所属套件、编号与详情页不符等）。
- 未被套件列入：器件既未注明所属套件，也没有任何套件将其列入；仅当套件目录
  完整（无套件页 / 列表分页失败、无抓取截断）时才能下此结论。
- 无法确认：结论依赖缺失的套件记录时（套件详情或列表分页抓取失败、抓取截断），
  不直接判「归属不符 / 未被套件列入」，改判无法确认，并附信息缺口与失败地址。
  仅器件详情页失败不破坏套件目录完整性，已抓取器件的反向结论照常判定。
"""
from __future__ import annotations

from datetime import datetime
from urllib.parse import urlsplit

from models import CrawlResult, ItemRecord, SuiteRecord


def _is_item_url(url: str) -> bool:
    return "/item/" in urlsplit(url).path


def cross_check(result: CrawlResult) -> dict:
    suites: list[SuiteRecord] = result.suites
    items: list[ItemRecord] = result.items
    failed_urls = {f.url for f in result.failures}
    # 截断（页数上限等）意味着可能还有整套列表 / 套件没有抓到
    truncated = bool(result.notes)

    # 套件目录完整性：套件详情页或列表分页抓取失败、抓取截断都会使
    # 「目录里有哪些套件、每个套件列入了谁」变得不可信。
    # 注意：仅器件详情页（/item/）失败不计入——它不影响套件侧记录。
    catalog_gap_failures = [f for f in result.failures if not _is_item_url(f.url)]
    suite_catalog_incomplete = truncated or bool(catalog_gap_failures)
    incomplete = bool(result.failures) or truncated

    items_by_no: dict[str, ItemRecord] = {}
    items_by_url: dict[str, ItemRecord] = {}
    number_conflicts: list[dict] = []
    for it in items:
        if it.number in items_by_no:
            number_conflicts.append(
                {"number": it.number, "kind": "器件", "first": items_by_no[it.number], "second": it}
            )
        else:
            items_by_no[it.number] = it
        items_by_url.setdefault(it.url, it)
    suites_by_no: dict[str, SuiteRecord] = {}
    for s in suites:
        if s.number in suites_by_no:
            number_conflicts.append(
                {"number": s.number, "kind": "套件", "first": suites_by_no[s.number], "second": s}
            )
        else:
            suites_by_no[s.number] = s

    missing: list[dict] = []        # 缺记录
    mismatches: list[dict] = []     # 归属不符（已抓取页面直接证实）
    unverifiable: list[dict] = []   # 无法确认（正向：套件列出的器件缺详情）
    reverse_unverifiable: list[dict] = []  # 无法确认（反向：依赖缺失的套件记录）
    orphans: list[ItemRecord] = []  # 未被套件列入
    listed_by: dict[str, list[SuiteRecord]] = {it.number: [] for it in items}
    member_status: dict[tuple[str, str], tuple[str, ItemRecord | None]] = {}
    consistent_pairs = 0

    def gap_payload(preferred_urls: list[str | None]) -> dict:
        """汇总信息缺口：截断说明 + 相关失败地址（优先直接相关的，再补其余目录缺口）。"""
        gap_notes: list[str] = list(result.notes)
        related: list[str] = []
        for u in preferred_urls:
            if u and u in failed_urls and u not in related:
                related.append(u)
        others = [
            f.url for f in catalog_gap_failures
            if f.url not in related
        ]
        return {
            "gap_notes": gap_notes,
            "related_failures": [f for f in result.failures if f.url in related],
            "other_gap_failures": [f for f in result.failures if f.url in others],
        }

    # —— 正向：以套件列出的器件编号为纲，逐一核对 ——
    for s in suites:
        for m in s.members:
            key = (s.url, m.accession)
            if m.link and m.link in failed_urls:
                unverifiable.append(
                    {"direction": "正向", "suite": s, "accession": m.accession, "link": m.link,
                     "reason": "器件详情页抓取失败，无法核对",
                     "gap_notes": [],
                     "related_failures": [f for f in result.failures if f.url == m.link],
                     "other_gap_failures": []}
                )
                member_status[key] = ("无法确认", None)
                continue
            target = items_by_url.get(m.link) if m.link else None
            if target is not None and target.number != m.accession:
                # 两份已抓取页面（套件页 + 器件详情页）直接证实，不受其他抓取失败影响
                mismatches.append(
                    {"problem": f"套件所列编号「{m.accession}」与详情页实际编号「{target.number}」不一致",
                     "suite": s, "item": target, "caveat": False}
                )
                listed_by.setdefault(target.number, []).append(s)
                member_status[key] = ("编号不符", target)
                continue
            if target is None:
                target = items_by_no.get(m.accession)
            if target is None:
                if incomplete:
                    unverifiable.append(
                        {"direction": "正向", "suite": s, "accession": m.accession, "link": m.link,
                         "reason": "已抓取页面中未找到该编号；因存在抓取失败/截断，无法确认是否漏录",
                         "gap_notes": list(result.notes),
                         "related_failures": [],
                         "other_gap_failures": list(catalog_gap_failures)}
                    )
                    member_status[key] = ("无法确认", None)
                else:
                    missing.append({"suite": s, "accession": m.accession})
                    member_status[key] = ("缺记录", None)
                continue
            listed_by.setdefault(target.number, []).append(s)
            if target.suite_no is None:
                # 套件页与器件页均已抓取，直接证实
                mismatches.append(
                    {"problem": "器件未注明所属套件，但该套件将其列入",
                     "suite": s, "item": target, "caveat": False}
                )
                member_status[key] = ("归属不符", target)
            elif target.suite_no != s.number:
                # 套件页与器件页均已抓取，直接证实
                mismatches.append(
                    {"problem": f"器件标注所属套件为「{target.suite_no}」，与列入它的套件不符",
                     "suite": s, "item": target, "caveat": False}
                )
                member_status[key] = ("归属不符", target)
            else:
                consistent_pairs += 1
                member_status[key] = ("一致", target)

    # —— 反向：以器件标注的所属套件为纲，逐一核对 ——
    reverse_status: dict[str, str] = {}
    for it in items:
        if it.suite_no:
            s = suites_by_no.get(it.suite_no)
            if s is None:
                # 若已抓取的套件清单已直接列入该器件，则两份已抓取页面已经
                # 证实错挂（正向核对已报告），该结论优先，不受套件目录缺口影响。
                if listed_by.get(it.number):
                    reverse_status[it.url] = "归属不符"
                    continue
                # 器件标注的套件没有抓到记录。
                # 若套件目录不完整（套件页 / 列表分页失败或截断），缺失的套件
                # 本可能存在且本可能列入该器件 → 无法确认；目录完整时才是确定的错挂。
                if suite_catalog_incomplete:
                    payload = gap_payload([it.suite_link])
                    if any(f.url == it.suite_link for f in payload["related_failures"]):
                        reason = (
                            f"器件标注所属套件为「{it.suite_no}」，但该套件详情页抓取失败，"
                            "无法核对其器件清单"
                        )
                    else:
                        reason = (
                            f"器件标注所属套件为「{it.suite_no}」，已抓取页面中无该套件记录；"
                            "因套件目录抓取不完整（分页失败/截断），无法确认该套件是否存在、是否列入本器件"
                        )
                    reverse_unverifiable.append(
                        {"item": it, "suite_no": it.suite_no, "reason": reason, **payload}
                    )
                    reverse_status[it.url] = "无法确认"
                else:
                    mismatches.append(
                        {"problem": f"器件标注所属套件为「{it.suite_no}」，但完整目录中不存在该套件",
                         "suite": None, "item": it, "caveat": False}
                    )
                    reverse_status[it.url] = "归属不符"
            elif not any(
                mem.accession == it.number or (mem.link and mem.link == it.url)
                for mem in s.members
            ):
                # 套件页本身已成功抓取，其清单内容可信 → 两份已抓取页面直接证实
                mismatches.append(
                    {"problem": f"器件标注所属「{it.suite_no}」，但该套件未将其列入",
                     "suite": s, "item": it, "caveat": False}
                )
                reverse_status[it.url] = "归属不符"
            else:
                reverse_status[it.url] = "一致"
        else:
            # 器件未注明所属套件：只有在确认没有任何套件列入它时才有意义。
            if not listed_by.get(it.number):
                if suite_catalog_incomplete:
                    # 缺失的套件页本可能列入它 → 不能判「未被套件列入」
                    payload = gap_payload([it.suite_link])
                    reverse_unverifiable.append(
                        {
                            "item": it,
                            "suite_no": None,
                            "reason": "器件未注明所属套件，且已抓取套件均未将其列入；"
                                      "因套件目录抓取不完整，无法确认缺失的套件页是否将其列入",
                            **payload,
                        }
                    )
                    reverse_status[it.url] = "无法确认"
                else:
                    orphans.append(it)
                    reverse_status[it.url] = "未被套件列入"
            else:
                reverse_status[it.url] = "归属不符"

    mismatch_item_urls = {row["item"].url for row in mismatches if row.get("item")}
    orphan_urls = {it.url for it in orphans}
    reverse_unverifiable_urls = {row["item"].url for row in reverse_unverifiable}

    suite_rows = []
    for s in suites:
        members = []
        for m in s.members:
            status, target = member_status.get((s.url, m.accession), ("—", None))
            members.append({"acc": m.accession, "link": m.link, "status": status, "item": target})
        suite_rows.append({"suite": s, "members": members})

    item_rows = []
    for it in items:
        lbs = listed_by.get(it.number, [])
        if it.url in reverse_unverifiable_urls:
            status = "无法确认"
        elif it.url in orphan_urls:
            status = "未被套件列入"
        elif it.url in mismatch_item_urls:
            status = "归属不符"
        elif lbs and it.suite_no and any(s.number == it.suite_no for s in lbs):
            status = "一致"
        else:
            status = "—"
        item_rows.append({"item": it, "listed_by": lbs, "status": status})

    return {
        "start_url": result.start_url,
        "elapsed": result.elapsed,
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "incomplete": incomplete,
        "suite_catalog_incomplete": suite_catalog_incomplete,
        "notes": result.notes,
        "failures": result.failures,
        "missing": missing,
        "mismatches": mismatches,
        "orphans": orphans,
        "unverifiable": unverifiable,
        "reverse_unverifiable": reverse_unverifiable,
        "duplicates": result.duplicates,
        "number_conflicts": number_conflicts,
        "suite_rows": suite_rows,
        "item_rows": item_rows,
        "list_pages": result.list_pages,
        "stats": {
            "list_pages": len(result.list_pages),
            "suites": len(suites),
            "items": len(items),
            "declared": sum(len(s.members) for s in suites),
            "consistent": consistent_pairs,
        },
    }
