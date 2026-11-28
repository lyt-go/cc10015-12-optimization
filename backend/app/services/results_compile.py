"""成果汇编领域服务：汇总航次全部成果，接受数据传输完成后的数据完整度更新联动。

补传场景下，汇编报告保存三样东西，互相独立、都不会被「退回补充」清掉：

* ``补传回执``：补传候选队列办理领取时的回执历史（一份失败传输一条）；
* ``补件凭据``：当前唯一有效、真正支撑本报告的传输尝试；``补件凭据记录`` 保存历史；
* ``失败传输定位`` / ``最近失败传输``：实际失败的那条传输记录，退回补充后仍然可查。
"""
from __future__ import annotations

from typing import Any

from app.services.base import BaseModuleService
from app.store import store


class ResultsCompileService(BaseModuleService):
    MODULE = "results_compile"
    ENTITY = "汇编报告"
    OPERATION_TERM = "提交汇编"
    LIST_FIELDS = ["航次编号", "成果类别", "主要发现", "参与人员", "数据完整度", "汇编人", "完成日期", "备注说明"]
    REQUIRED_FIELDS = ["航次编号", "成果类别", "主要发现"]
    STATUS_ORDER = ["汇编中", "已提交", "已审核", "需补充"]
    ACTION_RULES = {"提交汇编": "已提交", "审核通过": "已审核", "退回补充": "需补充"}
    NEGATIVE_ACTIONS = ["退回补充"]

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return None, f"{self.ENTITY} {entry_id} 不存在或已归档"

        target = self.ACTION_RULES.get(action)
        if target is None:
            return None, f"动作「{action}」不属于{self.MODULE}可执行范围"

        # 幂等检查
        idempotent_key = f"{self.MODULE}:{entry_id}:{action}"
        if store.is_idempotent(idempotent_key):
            return None, f"动作「{action}」已执行，请勿重复操作"

        entry["status"] = target
        entry["pending"] = target in ("汇编中", "需补充")
        entry["abnormal"] = action in self.NEGATIVE_ACTIONS

        if action == "退回补充":
            # 退回补充只是把报告打回，补传回执、失败传输定位和补件凭据都保留，
            # 这样值班员仍然能顺着报告定位到实际失败的那条传输。
            entry["补件状态"] = "退回补充"
            entry["退回次数"] = int(entry.get("退回次数", 0)) + 1
            entry["退回后仍可定位传输"] = True
            if entry.get("补件凭据"):
                entry["补件凭据"]["退回时保留"] = True

        store.mark_idempotent(idempotent_key)

        return entry, f"{self.ENTITY}已{action}"

    # ------------------------------------------------------------------
    # 补传候选队列 -> 汇编报告的回执与凭据
    # ------------------------------------------------------------------
    def record_supplement_receipt(self, report_id: int, receipt: dict[str, Any]) -> dict[str, Any] | None:
        """把补传领取回执挂到报告上，并记住实际失败的传输记录。

        重复提交同一份回执只更新、不追加，保证「报告只核收本次尝试的一份资料」。
        """
        report = store.find(self.MODULE, report_id)
        if report is None:
            return None

        stored = dict(receipt)
        receipts: list[dict[str, Any]] = report.setdefault("补传回执", [])
        existing = next(
            (item for item in receipts if item.get("回执号") == stored.get("回执号")),
            None,
        )
        if existing is not None:
            existing.update(stored)
            stored = existing
        else:
            receipts.append(stored)

        failed = {
            "传输记录id": stored.get("原传输记录id"),
            "attempt_id": stored.get("原传输尝试"),
            "目标系统": stored.get("目标系统"),
            "数据内容": stored.get("数据内容"),
            "航次编号": stored.get("航次编号"),
            "轮次": stored.get("原传输轮次"),
            "回执号": stored.get("回执号"),
            "状态": "已失败",
        }
        report["最近失败传输"] = failed
        locations: list[dict[str, Any]] = report.setdefault("失败传输定位", [])
        if not any(
            item.get("传输记录id") == failed["传输记录id"]
            and item.get("目标系统") == failed["目标系统"]
            for item in locations
        ):
            locations.append(failed)

        report["补件状态"] = "已领取待回执"
        report["pending"] = True
        if report.get("补件凭据", {}).get("有效") and report.get("transfer_complete"):
            # 其它目标系统的失败记录被领取，不影响报告已由岸端核收的状态。
            report["补件状态"] = "已接收（另有补传在办）"
            report["pending"] = False
        return stored

    def mark_supplement_receipt(
        self, report_id: int, receipt_id: str, updates: dict[str, Any]
    ) -> dict[str, Any] | None:
        """更新报告中某份补传回执的办理结果（只更新对应回执）。"""
        report = store.find(self.MODULE, report_id)
        if report is None:
            return None
        for item in report.get("补传回执", []):
            if item.get("回执号") == receipt_id:
                item.update(updates)
                return item
        return None

    def bind_supplement_credential(
        self, report_id: int, credential: dict[str, Any]
    ) -> dict[str, Any] | None:
        """把唯一有效传输尝试绑定为报告补件凭据。

        - 报告同一时刻只保留一份 ``补件凭据``；已经有一份有效凭据时，
          后来的尝试只记为重复来件，不会把有效凭据顶掉；
        - 同一目标系统已经核收过资料时，新的同目标尝试只记为重复来件，
          不会再次写入数据完整度（报告只核收一份资料）。
        """
        report = store.find(self.MODULE, report_id)
        if report is None:
            return None

        cred = dict(credential)
        attempt_key = cred.get("attempt_id")
        history: list[dict[str, Any]] = report.setdefault("补件凭据记录", [])
        current = report.get("补件凭据") or {}

        if current.get("有效") and current.get("attempt_id") == attempt_key:
            current["重复绑定"] = True
            return current

        if current.get("有效"):
            # 报告已经有唯一有效凭据：后来的尝试（无论是重复领取的新尝试，
            # 还是领取完成后才到的旧连接确认）都只登记为重复来件，不顶掉凭据。
            cred["有效"] = False
            cred["重复来件"] = True
            cred["说明"] = "报告已由另一份有效传输尝试核收，本次仅登记为重复来件"
            history.append(cred)
            return cred

        for item in history:
            item["有效"] = False

        target = str(cred.get("目标系统") or "")
        received_targets: list[str] = report.setdefault("已接收目标系统", [])
        already_received = target in received_targets
        cred["重复来件"] = already_received
        cred["有效"] = True

        history.append(cred)
        report["补件凭据"] = cred
        report["补件轮次"] = cred.get("轮次")
        report["补件接收来源"] = cred.get("来源")
        report["补件状态"] = "已接收"

        if already_received:
            return cred

        current_text = str(report.get("数据完整度", ""))
        content = cred.get("数据内容", "")
        round_no = cred.get("轮次", "")
        report["数据完整度"] = (
            f"{current_text} | 已接收：{content}（{target}·第{round_no}轮）"
        )
        received_targets.append(target)
        report["transfer_complete"] = True
        report["last_transfer_time"] = cred.get("接收时间", "")
        report["pending"] = False
        return cred

    def record_archive_received(self, report_id: int, transfer: dict[str, Any]) -> dict[str, Any] | None:
        """登记科研归档端已收到资料。

        科研归档端与岸端是两套接收系统：这里只做登记，
        不写补件凭据、不置 transfer_complete，也不替代岸端接收。
        """
        report = store.find(self.MODULE, report_id)
        if report is None:
            return None
        archive = {
            "传输记录id": transfer.get("id"),
            "attempt_id": transfer.get("attempt_id"),
            "轮次": transfer.get("轮次"),
            "数据内容": transfer.get("数据内容"),
            "目标系统": transfer.get("目标系统"),
            "接收时间": transfer.get("完成时间") or transfer.get("传输时间", ""),
        }
        report["科研归档接收"] = archive
        note = "科研归档端已收到，不能替代岸端接收"
        report["科研归档说明"] = note
        report["补件状态"] = "待岸端接收" if not report.get("transfer_complete") else report.get("补件状态")
        return archive

    def supplement_info(self, report_id: int) -> dict[str, Any] | None:
        """报告补件视图：当前凭据、回执历史、失败传输定位。"""
        report = store.find(self.MODULE, report_id)
        if report is None:
            return None
        credential = report.get("补件凭据")
        return {
            "report_id": report.get("id"),
            "航次编号": report.get("航次编号"),
            "成果类别": report.get("成果类别"),
            "status": report.get("status"),
            "补件状态": report.get("补件状态"),
            "补件凭据": credential,
            "credential": credential,
            "补件凭据记录": report.get("补件凭据记录", []),
            "补传回执": report.get("补传回执", []),
            "receipts": report.get("补传回执", []),
            "失败传输定位": report.get("失败传输定位", []),
            "最近失败传输": report.get("最近失败传输"),
            "已接收目标系统": report.get("已接收目标系统", []),
            "科研归档接收": report.get("科研归档接收"),
            "科研归档说明": report.get("科研归档说明"),
        }

    def update_completeness(self, entry_id: int, data_source: str, data_desc: str) -> tuple[dict[str, Any] | None, str]:
        """由数据传输模块联动更新数据完整度。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return None, f"{self.ENTITY} {entry_id} 不存在"
        current = entry.get("数据完整度", "")
        entry["数据完整度"] = f"{current} | [{data_source}] {data_desc}"
        entry["pending"] = True
        return entry, "数据完整度已更新"

    def get_completeness_summary(self) -> dict[str, Any]:
        """获取整体数据完整度摘要。"""
        rows = store.rows(self.MODULE)
        total = len(rows)
        complete = sum(1 for r in rows if r.get("transfer_complete"))
        return {
            "total_reports": total,
            "complete_transfers": complete,
            "completeness_pct": round(complete / total * 100, 1) if total > 0 else 0,
        }
