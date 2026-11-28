"""数据传输领域服务：完成传输后自动联动成果汇编更新数据完整度。

补传办理约定：

* 一条 ``已失败`` 的传输记录就是一份补传候选，按它自己的 ``目标系统`` 判定重试资格；
* 科研归档端和岸端分别核对，任一端已收到都不能替另一端消除候选；
* ``传输中`` / ``待传输`` 这类未失败、仍活动的轮次永远不会进入候选；
* 领取补传会新开一条尝试记录（轮次 +1），原失败记录保留，回执同时挂到汇编报告；
* 同一份失败记录只能被领取一次，重复提交返回首次领取结果，不重复建档；
* 旧连接迟到的成功确认落在原传输上，与已领取的新尝试冲突时，新尝试作废，
  报告凭据明确记录接收的是「原传输」还是「领取补传」。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from app.services.base import BaseModuleService
from app.services.results_compile import ResultsCompileService
from app.store import store

ARCHIVE_HINTS = ("归档", "科研")


def target_kind(target: Any) -> str:
    """把目标系统归成科研归档端 / 岸端两类，其它目标按岸端资料处理。"""
    text = str(target or "")
    return "archive" if any(hint in text for hint in ARCHIVE_HINTS) else "shore"


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


class DataTransferService(BaseModuleService):
    MODULE = "data_transfer"
    ENTITY = "传输记录"
    OPERATION_TERM = "发起传输"
    LIST_FIELDS = ["数据内容", "数据量", "传输方式", "目标系统", "传输状态", "操作人", "传输时间", "备注说明"]
    REQUIRED_FIELDS = ["数据内容", "数据量", "传输方式"]
    STATUS_ORDER = ["待传输", "传输中", "已完成", "已失败"]
    ACTION_RULES = {"发起传输": "传输中", "确认完成": "已完成", "重试传输": "待传输"}
    NEGATIVE_ACTIONS = []
    ACTIVE_STATUSES = ("待传输", "传输中")
    SPECIAL_ACTIONS = ("领取补传", "迟到确认")

    # ------------------------------------------------------------------
    # 通用动作
    # ------------------------------------------------------------------
    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        """登记传输记录，允许附带航次编号 / 报告id 这类关联字段。"""
        entry, missing = super().create_entry(values)
        if entry is None:
            return entry, missing
        for field in ("航次编号", "报告id", "report_id"):
            if values.get(field) not in (None, ""):
                entry[field] = values[field]
        entry["轮次"] = 1
        entry["attempt_id"] = self._attempt_id(entry)
        entry["领取状态"] = "未领取"
        return entry, missing

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return None, f"{self.ENTITY} {entry_id} 不存在或已归档"

        self._ensure_fields(entry)

        if action == "领取补传":
            claim, message, _ = self.claim_supplement(entry_id)
            if claim is None:
                return None, message
            successor = store.find(self.MODULE, int(claim["新尝试记录id"]))
            return successor, message

        if action == "迟到确认":
            updated, message, _ = self.replay_late_confirmation(entry_id)
            return updated, message

        target = self.ACTION_RULES.get(action)
        if target is None:
            return None, f"动作「{action}」不属于{self.MODULE}可执行范围"

        idempotent_key = f"{self.MODULE}:{entry_id}:{action}"
        if store.is_idempotent(idempotent_key):
            return None, f"动作「{action}」已执行，请勿重复操作"

        if action == "重试传输":
            if entry.get("status") != "已失败":
                return None, "只能重试已失败的传输"
            if self._target_received(entry, exclude_id=entry_id):
                return None, f"{entry.get('目标系统', '目标系统')}已收到资料，无需重试"
            entry["retry_count"] = int(entry.get("retry_count", 0)) + 1
            entry["last_retry_time"] = _now()
            entry["重试开始时间"] = entry["last_retry_time"]

        if action == "确认完成":
            if entry.get("status") == "已失败":
                return None, "已失败的传输不能直接确认完成，请重试或由旧连接回放确认"
            store.save_snapshot(f"{self.MODULE}:{entry_id}:confirm", self.MODULE, entry_id)
            source = "领取补传" if entry.get("是否补传") else "原传输"
            self._complete_attempt(entry, source=source)
            store.mark_idempotent(idempotent_key)
            return entry, f"{self.ENTITY}已{action}"

        entry["status"] = target
        entry["pending"] = target in self.ACTIVE_STATUSES
        entry["abnormal"] = False

        store.mark_idempotent(idempotent_key)
        return entry, f"{self.ENTITY}已{action}"

    # ------------------------------------------------------------------
    # 字段归一化 / 尝试标识
    # ------------------------------------------------------------------
    def _attempt_id(self, entry: dict[str, Any]) -> str:
        round_no = int(entry.get("轮次", 1))
        return str(entry.get("attempt_id") or f"TX-{int(entry.get('id', 0)):04d}-R{round_no}")

    def _ensure_fields(self, entry: dict[str, Any]) -> None:
        entry.setdefault("轮次", 1)
        entry["attempt_id"] = self._attempt_id(entry)
        entry.setdefault("原传输记录id", None)
        entry.setdefault("领取状态", "已领取" if entry.get("领取回执号") else "未领取")
        entry.setdefault("航次编号", entry.get("航次编号") or "")

    def _all_rows(self) -> list[dict[str, Any]]:
        return store.rows(self.MODULE)

    # ------------------------------------------------------------------
    # 目标系统维度的收录 / 重试资格
    # ------------------------------------------------------------------
    def _same_scope(self, row: dict[str, Any], candidate: dict[str, Any]) -> bool:
        """同一航次（有航次号时）+ 同一数据内容，才算同一批资料。"""
        if str(row.get("数据内容", "")) != str(candidate.get("数据内容", "")):
            return False
        row_voyage = str(row.get("航次编号") or "")
        cand_voyage = str(candidate.get("航次编号") or "")
        if row_voyage and cand_voyage:
            return row_voyage == cand_voyage
        return True

    def _target_received(
        self,
        entry: dict[str, Any],
        *,
        target: str | None = None,
        exclude_id: int | None = None,
    ) -> dict[str, Any] | None:
        """检查同一航次、同一资料、指定目标系统是否已经有成功接收的传输。"""
        target = target if target is not None else str(entry.get("目标系统") or "")
        for row in self._all_rows():
            if exclude_id is not None and int(row.get("id", 0)) == int(exclude_id):
                continue
            if str(row.get("目标系统") or "") != target:
                continue
            if row.get("status") != "已完成" and row.get("接收状态") != "已接收":
                continue
            if self._same_scope(entry, row):
                return row
        return None

    def retry_eligible(self, entry: dict[str, Any]) -> bool:
        """这条失败记录在它自己的目标系统上还有没有补传资格。"""
        if entry.get("status") != "已失败" or entry.get("被取代"):
            return False
        return self._target_received(entry, exclude_id=int(entry.get("id", 0))) is None

    def _has_active_round(self, entry: dict[str, Any]) -> bool:
        """同一目标系统是否已经有正在进行的重试（仅作参考，不隐藏失败候选）。"""
        for row in self._all_rows():
            if int(row.get("id", 0)) == int(entry.get("id", 0)):
                continue
            if row.get("status") not in self.ACTIVE_STATUSES:
                continue
            if str(row.get("目标系统") or "") != str(entry.get("目标系统") or ""):
                continue
            if row.get("是否补传") or row.get("原传输记录id"):
                continue
            if self._same_scope(entry, row):
                return True
        return False

    # ------------------------------------------------------------------
    # 补传候选队列
    # ------------------------------------------------------------------
    def list_candidates(
        self,
        *,
        report_id: int | None = None,
        voyage: str | None = None,
        target: str | None = None,
        data_content: str | None = None,
        include_claimed: bool = False,
    ) -> list[dict[str, Any]]:
        """返回可办理的补传候选。

        ``report_id`` 用来把队列限定到该汇编报告的航次，岸端值班员据此定位可处理资料。
        """
        if report_id is not None:
            report = store.find("results_compile", report_id)
            if report is None:
                return []
            voyage = voyage or str(report.get("航次编号") or "")

        items: list[dict[str, Any]] = []
        for row in self._all_rows():
            self._ensure_fields(row)
            if row.get("status") != "已失败":
                # 未失败且仍活动的一轮不能作为可重试候选。
                continue
            if row.get("被取代"):
                continue
            if voyage and str(row.get("航次编号") or "") != str(voyage):
                continue
            if target and str(row.get("目标系统") or "") != str(target):
                continue
            if data_content and str(row.get("数据内容") or "") != str(data_content):
                continue
            claimed = row.get("领取状态") == "已领取"
            if claimed and not include_claimed:
                continue
            if not self.retry_eligible(row):
                continue
            view = self._candidate_view(row)
            if self._has_active_round(row):
                view["已有进行中轮次"] = True
            items.append(view)
        return items

    def _candidate_view(self, row: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": row.get("id"),
            "记录id": row.get("id"),
            "transfer_id": row.get("id"),
            "attempt_id": self._attempt_id(row),
            "航次编号": row.get("航次编号", ""),
            "数据内容": row.get("数据内容", ""),
            "数据量": row.get("数据量", ""),
            "传输方式": row.get("传输方式", ""),
            "目标系统": row.get("目标系统", ""),
            "target_system": row.get("目标系统", ""),
            "轮次": int(row.get("轮次", 1)),
            "status": row.get("status"),
            "传输状态": row.get("传输状态", ""),
            "失败原因": row.get("失败原因", row.get("备注说明", "")),
            "传输时间": row.get("传输时间", ""),
            "操作人": row.get("操作人", ""),
            "retry_eligible": True,
            "重试资格": True,
            "claimed": row.get("领取状态") == "已领取",
            "领取状态": row.get("领取状态", "未领取"),
            "领取回执号": row.get("领取回执号"),
        }

    def candidate_summary(self, **kwargs: Any) -> dict[str, Any]:
        """候选队列摘要，额外按目标系统分组，便于岸端值班员分头核对。"""
        items = self.list_candidates(**kwargs)
        by_target: dict[str, int] = {}
        for item in items:
            key = str(item.get("目标系统") or "未指定")
            by_target[key] = by_target.get(key, 0) + 1
        return {
            "items": items,
            "candidates": items,
            "total": len(items),
            "by_target": by_target,
            "counts": by_target,
        }

    # ------------------------------------------------------------------
    # 补传领取
    # ------------------------------------------------------------------
    def _find_claim(self, entry_id: int) -> dict[str, Any] | None:
        for claim in store.rows("supplement_claim"):
            if int(claim.get("原传输记录id") or 0) == int(entry_id):
                return claim
        return None

    def claim_supplement(
        self,
        entry_id: int,
        *,
        report_id: int | None = None,
        operator: str = "岸端值班员",
        receipt_id: str | None = None,
        target: str | None = None,
        idempotency_token: str | None = None,
    ) -> tuple[dict[str, Any] | None, str, bool]:
        """领取一条失败传输的补传任务。

        返回 ``(领取回执, 消息, 是否重复提交)``。重复提交不会新建尝试，也不会重复挂回执。
        """
        lock = store.lock_for(f"{self.MODULE}:claim:{entry_id}")
        with lock:
            row = store.find(self.MODULE, entry_id)
            if row is None:
                return None, f"{self.ENTITY} {entry_id} 不存在或已归档", False
            self._ensure_fields(row)

            existing = self._find_claim(entry_id)
            if existing is not None:
                message = "补传领取已办理，重复提交只记一次"
                if row.get("status") == "已完成":
                    message = "原传输已由旧连接确认完成，本次领取已作废"
                return existing, message, True

            if row.get("status") != "已失败":
                return None, "只有已失败、且没有进行中重试的传输可以领取补传", False
            if row.get("被取代"):
                return None, "该失败记录已被新的尝试取代，不能重复领取", False

            row_target = str(row.get("目标系统") or "")
            if target and row_target and target != row_target:
                return None, f"候选目标系统为「{row_target}」，与领取目标「{target}」不一致", False
            resolved_target = row_target or str(target or "")

            received = self._target_received(row, target=resolved_target, exclude_id=entry_id)
            if received is not None:
                return None, f"{resolved_target or '目标系统'}已收到本批资料，无需补传", False
            concurrent_active = self._has_active_round(row)

            resolved_report_id = self._resolve_report_id(row, report_id)

            with store.lock_for(f"{self.MODULE}:ids"):
                new_id = store.next_id(self.MODULE)
                new_round = int(row.get("轮次", 1)) + 1
                receipt_no = receipt_id or f"SUP-{int(entry_id):04d}-R{new_round}"
                attempt_id = f"TX-{new_id:04d}-R{new_round}"
                new_attempt: dict[str, Any] = {
                    "id": new_id,
                    "status": "待传输",
                    "pending": True,
                    "abnormal": False,
                    "数据内容": row.get("数据内容", ""),
                    "数据量": row.get("数据量", ""),
                    "传输方式": row.get("传输方式", ""),
                    "目标系统": resolved_target,
                    "传输状态": "待补传",
                    "操作人": operator,
                    "传输时间": "",
                    "备注说明": f"由失败传输{entry_id}领取补传，第{new_round}轮",
                    "轮次": new_round,
                    "attempt_id": attempt_id,
                    "原传输记录id": entry_id,
                    "原传输尝试": self._attempt_id(row),
                    "原传输轮次": int(row.get("轮次", 1)),
                    "领取回执号": receipt_no,
                    "领取人": operator,
                    "领取时间": _now(),
                    "领取状态": "已领取",
                    "是否补传": True,
                    "并发轮次提示": "同一目标系统另有进行中的轮次" if concurrent_active else "",
                    "航次编号": row.get("航次编号", ""),
                    "报告id": resolved_report_id,
                }
                store.atomic_append(self.MODULE, new_attempt)

            row["领取状态"] = "已领取"
            row["领取回执号"] = receipt_no
            row["领取人"] = operator
            row["领取时间"] = _now()
            row["承接尝试id"] = new_id
            row["承接尝试"] = attempt_id
            row["pending"] = False

            claim = {
                "id": store.next_id("supplement_claim"),
                "回执号": receipt_no,
                "receipt_id": receipt_no,
                "原传输记录id": entry_id,
                "原传输尝试": self._attempt_id(row),
                "原传输轮次": int(row.get("轮次", 1)),
                "新尝试记录id": new_id,
                "新尝试attempt_id": attempt_id,
                "轮次": new_round,
                "报告id": resolved_report_id,
                "reported_report_id": resolved_report_id,
                "航次编号": row.get("航次编号", ""),
                "数据内容": row.get("数据内容", ""),
                "目标系统": resolved_target,
                "值班员": operator,
                "operator": operator,
                "领取时间": _now(),
                "办理状态": "待回执",
                "idempotency_token": idempotency_token,
            }
            store.atomic_append("supplement_claim", claim)

            receipt = dict(claim)
            receipt.update({
                "报告id": resolved_report_id,
                "数据内容": row.get("数据内容", ""),
                "航次编号": row.get("航次编号", ""),
                "原传输状态": "已失败",
                "新尝试状态": "待传输",
            })
            if resolved_report_id is not None:
                ResultsCompileService().record_supplement_receipt(resolved_report_id, receipt)

            return claim, f"{row.get('目标系统', '')}补传已领取，回执号 {receipt_no}", False

    def _resolve_report_id(self, row: dict[str, Any], report_id: int | None) -> int | None:
        if report_id is not None and store.find("results_compile", int(report_id)) is not None:
            return int(report_id)
        linked = row.get("报告id") or row.get("report_id")
        if linked not in (None, ""):
            return int(linked)
        voyage = str(row.get("航次编号") or "")
        if not voyage:
            return None
        for report in store.rows("results_compile"):
            if str(report.get("航次编号") or "") == voyage:
                return int(report.get("id"))
        return None

    # ------------------------------------------------------------------
    # 完成 / 旧连接迟到确认
    # ------------------------------------------------------------------
    def replay_late_confirmation(
        self,
        entry_id: int,
        *,
        report_id: int | None = None,
        confirmed_at: str | None = None,
    ) -> tuple[dict[str, Any] | None, str, bool]:
        """旧连接恢复后回放迟到的成功确认，返回 (记录, 消息, 是否迟到)。

        原传输因此变为已完成；若此前已被领取补传，则领取出来的新尝试作废，
        报告凭据按「原传输」登记，并保留冲突说明。
        """
        lock = store.lock_for(f"{self.MODULE}:late:{entry_id}")
        with lock:
            row = store.find(self.MODULE, entry_id)
            if row is None:
                return None, f"{self.ENTITY} {entry_id} 不存在或已归档", False
            self._ensure_fields(row)

            if row.get("status") == "已完成":
                return row, "该传输已有成功确认，迟到确认按重复来件处理", True

            if row.get("status") not in ("已失败", "传输中"):
                return None, "只有已失败或仍传输中的旧连接可以回放成功确认", False

            if row.get("status") == "传输中":
                # 仍活动的一轮本来就不在候选里，直接补上成功确认即可。
                self._complete_attempt(row, source="原传输", confirmed_at=confirmed_at)
                return row, "旧连接恢复，传输已确认完成", False

            self._complete_attempt(
                row,
                source="原传输",
                late=True,
                confirmed_at=confirmed_at,
                report_id=report_id,
            )
            return row, "旧连接迟到确认已回放，原传输已确认完成", True

    def _complete_attempt(
        self,
        row: dict[str, Any],
        *,
        source: str,
        late: bool = False,
        confirmed_at: str | None = None,
        report_id: int | None = None,
    ) -> None:
        received_at = confirmed_at or _now()
        row["status"] = "已完成"
        row["pending"] = False
        row["abnormal"] = False
        row["完成时间"] = received_at
        row["接收状态"] = "已接收"
        row["确认来源"] = "旧连接迟到确认" if late else source
        if late:
            row["迟到确认"] = True

        self._update_report_after_completion(row, source=source, late=late, report_id=report_id)
        # 冲突处理放在报告联动之后，保证回执上的「冲突作废」不会被接收状态覆盖。
        self._settle_claim_conflict(row)

    def _settle_claim_conflict(self, row: dict[str, Any]) -> None:
        """原传输完成时，把此前领取出来、尚未完成的新尝试标记作废。"""
        successor_id = row.get("承接尝试id")
        if successor_id is None:
            return
        successor = store.find(self.MODULE, int(successor_id))
        if successor is None:
            return
        if successor.get("status") in ("已完成", "已取消"):
            return
        successor["status"] = "已取消"
        successor["被取代"] = True
        successor["pending"] = False
        successor["冲突处理"] = "原传输由旧连接迟到确认完成，本次领取的补传尝试作废"
        successor["作废时间"] = _now()

        receipt_no = row.get("领取回执号")
        if receipt_no:
            updates = {
                "办理状态": "冲突作废",
                "新尝试状态": "已取消",
                "冲突说明": successor["冲突处理"],
                "处理时间": successor["作废时间"],
            }
            for report in store.rows("results_compile"):
                ResultsCompileService().mark_supplement_receipt(
                    int(report.get("id")), str(receipt_no), updates
                )

    def _linked_reports(self, row: dict[str, Any], report_id: int | None = None) -> list[dict[str, Any]]:
        if report_id is not None:
            report = store.find("results_compile", int(report_id))
            return [report] if report is not None else []
        linked = row.get("报告id") or row.get("report_id")
        if linked not in (None, ""):
            report = store.find("results_compile", int(linked))
            if report is not None:
                return [report]
        voyage = str(row.get("航次编号") or "")
        if voyage:
            matched = [
                report for report in store.rows("results_compile")
                if str(report.get("航次编号") or "") == voyage
            ]
            if matched:
                return matched
        # 没有航次关联时保持原有的通用联动：当前在编的报告都更新。
        return [
            report for report in store.rows("results_compile")
            if report.get("status") in ("汇编中", "已提交", "需补充")
        ]

    def _update_report_after_completion(
        self,
        row: dict[str, Any],
        *,
        source: str,
        late: bool,
        report_id: int | None = None,
    ) -> None:
        reports = self._linked_reports(row, report_id)
        service = ResultsCompileService()
        kind = target_kind(row.get("目标系统"))
        for report in reports:
            report_id_value = int(report.get("id"))
            if kind == "archive":
                # 科研归档端只登记归档接收，不能替代岸端接收，也不成为报告补件凭据。
                service.record_archive_received(report_id_value, row)
                continue
            credential = {
                "attempt_id": self._attempt_id(row),
                "transfer_id": row.get("id"),
                "轮次": int(row.get("轮次", 1)),
                "目标系统": row.get("目标系统", ""),
                "数据内容": row.get("数据内容", ""),
                "航次编号": row.get("航次编号", ""),
                "来源": source,
                "迟到确认": late,
                "领取回执号": row.get("领取回执号"),
                "接收时间": row.get("完成时间", ""),
            }
            service.bind_supplement_credential(report_id_value, credential)

        receipt_no = row.get("领取回执号")
        if receipt_no:
            for report in reports:
                service.mark_supplement_receipt(
                    int(report.get("id")),
                    str(receipt_no),
                    {
                        "办理状态": "已接收",
                        "新尝试状态": "已完成",
                        "接收时间": row.get("完成时间", ""),
                        "接收来源": source,
                    },
                )

        row["compile_updates"] = len(reports)

    def _update_results_completeness(self, entry: dict[str, Any]) -> None:
        """保留旧接口：完成传输后更新成果汇编数据完整度。"""
        self._update_report_after_completion(entry, source="原传输", late=False)
