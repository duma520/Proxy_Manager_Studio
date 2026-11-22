# -*- coding: utf-8 -*-
"""延迟与测速（跑测试、进度条、结果回填、断点续跑）——v1.9.36 从 `pms/gui/mainwindow.py` 拆出的 mixin。

★ 拆分原则：**方法名与 `self.pmw_*` 属性名一个字都没改**，外部引用（主窗口之外用到
`pmw_*` 的 80 处）全都不用动；mixins 只是把方法按功能域挪个文件。
"""
from __future__ import annotations

from PySide6.QtWidgets import QMenu, QMessageBox
from pms.constants import PMS_APP_NAME, PMS_TEST_MODE_TCP, PMS_TEST_MODE_URL
from pms.models import PmsProxyNode
from pms.tasks import PmsTcpPingSignals, PmsTcpPingTask
from pms.utils import pms_format_bytes, pms_format_speed, pms_now_millis_text, pms_now_text
from typing import Sequence


class _PmwTestMixin:
    """延迟与测速（跑测试、进度条、结果回填、断点续跑、选择测试弹窗）（47 个方法）。"""

    # v1.9.59：断点类型的中文名（续跑菜单 / 按钮文案统一走这张表，别再写三元）。
    _PMW_ROUND_KIND_TEXT = {"latency": "延迟", "speed": "测速", "telegram_dc": "电报"}

    def pmw_round_kind_text(self, pms_kind: str) -> str:
        return self._PMW_ROUND_KIND_TEXT.get(str(pms_kind or ""), "测速")

    def pmw_stop_tests(self, pms_kind: str = "") -> None:
        """一键停止测试（v1.9.17）：`pms_kind` = ""（全部）/ latency / speed。

        与「退出程序」不同，这里是**暂停**：已测结果保留、剩下的写进断点，
        之后可以点「继续上次测试」接着跑（`plt_stop()` / `pst_stop()` 的既有语义）。
        """
        want_latency = pms_kind in ("", "latency")
        want_speed = pms_kind in ("", "speed")
        stopped: list[str] = []
        if want_latency:
            # v1.9.37：健康检查也占用延迟测试通道，但它是一次性轮次——先停它，别去停用户的手动测试
            health = getattr(self, "pmw_health", None)
            if health is not None and health.phs_is_running():
                health.phs_stop()
                stopped.append("健康检查")
            elif self.pmw_latency.plt_is_running():
                self.pmw_latency.plt_stop()
                stopped.append("延迟")
            # TCP 握手模式不走批量调度器（没有 running 标志），停止就是清空线程池
            if self.pmw_tcp_pool.activeThreadCount() > 0:
                self.pmw_tcp_pool.clear()
                stopped.append("TCP 延迟")
        if want_speed and self.pmw_speed.pst_is_running():
            self.pmw_speed.pst_stop()
            stopped.append("测速")
        # v1.9.59：电报 DC 测试（断点语义与测速一致：停 = 保留已测 + 可续跑）
        if pms_kind in ("", "telegram") and self.pmw_telegram.ptg_is_running():
            self.pmw_telegram.ptg_stop()
            stopped.append("电报测试")
        if stopped:
            self.pmw_end_test_progress()
        self.pmw_refresh_resume_action()
        self.pmw_show_message("已停止：" + "、".join(stopped) + "（已测结果保留，可「继续上次测试」）" if stopped else "当前没有在跑的测试")

    def pmw_pick_test_nodes(self, pms_scope: str) -> list[PmsProxyNode]:
        """挑选测试范围：selected（选中）/ page（当前页）/ all（全部筛选结果，跨页）。

        v1.7.0：分页后新增 page。「all」必须仍是**全部筛选结果**，
        自动测速（pmw_on_auto_test_tick）走的就是它，语义不能变。
        v1.9.61：新增 `untested` —— 「测新增的」专用；在 selected/page/all 的基础范围之上，
        再额外裁掉已经测过的节点（见 `PmsProxyNode.pnode_is_untested()`，任一未测才算），
        其余去重 / 仅测启用 的过滤与基础范围一致。
        """
        if pms_scope.endswith("_untested"):
            base_scope = pms_scope[: -len("_untested")]
            nodes = self.pmw_pick_test_nodes(base_scope)
            nodes = [node for node in nodes if node.pnode_is_untested()]
            return nodes
        if pms_scope == "selected":
            nodes = self.pmw_selected_nodes()
            if not nodes:
                nodes = self.pmw_visible_nodes()
        elif pms_scope == "page":
            nodes = self.pmw_page_nodes() or self.pmw_visible_nodes()
        else:
            nodes = self.pmw_visible_nodes() or self.pmw_nodes
        if self.pmw_settings.psm_get_bool("test.only_test_enabled", False):
            nodes = [node for node in nodes if node.enabled]
        if self.pmw_settings.psm_get_bool("test.remove_duplicates_before_test", True):
            seen: set[str] = set()
            unique: list[PmsProxyNode] = []
            for node in nodes:
                key = node.pnode_endpoint_signature()
                if key in seen:
                    continue
                seen.add(key)
                unique.append(node)
            nodes = unique
        return nodes

    def pmw_effective_scope(self, pms_scope: str) -> str:
        """把「仅测新增」开关作用到范围上（v1.9.61）。

        勾了 `pmw_only_untested` 时，凡是基础范围（selected/page/all）都自动补 `_untested`，
        `pmw_pick_test_nodes` 会据此裁掉已测过的节点；已经带 `_untested` 的（一键跑三种 /
        「仅测新增」菜单项直接传的）原样返回，不重复追加。
        """
        if not getattr(self, "pmw_only_untested", False):
            return pms_scope
        if pms_scope.endswith("_untested"):
            return pms_scope
        return f"{pms_scope}_untested"

    def pmw_toggle_only_untested(self) -> None:
        """「仅测新增」勾选框：切换 `pmw_only_untested` 并同步按钮状态。"""
        self.pmw_only_untested = not getattr(self, "pmw_only_untested", False)
        action = self.pmw_actions.get("only_untested")
        if action is not None:
            action.setChecked(self.pmw_only_untested)
        self.pmw_show_message("已开启「仅测新增」：延迟/测速/电报只跑没测过的节点" if self.pmw_only_untested
                              else "已关闭「仅测新增」：恢复测全部节点")

    def pmw_progress_kind_title(self) -> str:
        """进度条上写的是哪一类任务（v1.9.22：进度条是**共用**的，所有会用到它的功能都在这里登记）。"""
        return {
            "latency": "延迟测试",
            "tcp": "TCP 延迟测试",
            "speed": "测速",
            "subscription": "订阅更新",
            "download": "核心下载",
            "import": "导入节点",
            "telegram": "电报抓取",
            "telegram_dc": "电报测试",
        }.get(self.pmw_progress_kind, "任务")

    def pmw_progress_text(self, pms_done: int, pms_detail: str = "") -> str:
        """进度条文本：`测速 12/31（38%）节点名`。

        v1.9.22：以前只写 `12/31 节点名`，单节点测试时是 `0/1`、`1/1`，看不出在测什么；
        现在带上类别与百分比，**单节点也一眼知道进度**。总数为 0（数量未知）时写「进行中」。
        """
        total = max(0, int(self.pmw_progress_total or 0))
        done = max(0, int(pms_done or 0))
        if total:
            percent = int(done * 100 / total) if total else 0
            text = f"{self.pmw_progress_kind_title()} {done}/{total}（{percent}%）"
        else:
            text = f"{self.pmw_progress_kind_title()} 进行中"
        detail = str(pms_detail or "").strip()
        return f"{text} {detail}" if detail else text

    def pmw_begin_test_progress(self, pms_kind: str, pms_total: int, pms_message: str = "") -> None:
        """任何一批任务开始前**统一**开进度条（v1.9.22）。

        修的是这一类问题：进度条原来散在各入口手动 `show()`——API 驱动的测试、批量操作的部分分支
        根本没开；而且它挂在「速览条」里，用户把速览条隐藏了（`ui.show_quick_stats`）就完全看不到进度。
        现在**所有会用到进度条的功能**（延迟 / 测速 / TCP / 订阅更新 / 核心下载 / 导入 / 电报抓取）
        都走这里：测试期间速览条若被设置隐藏，临时露出来，结束时再藏回去。
        """
        self.pmw_progress_kind = str(pms_kind or "")
        self.pmw_progress_total = max(0, int(pms_total or 0))
        self.pmw_progress_restore_quick = False
        if not self.pmw_quick_frame.isVisible() and not self.pmw_settings.psm_get_bool("ui.show_quick_stats", True):
            self.pmw_progress_restore_quick = True
            self.pmw_quick_frame.show()
        # 总数为 0 = 数量未知 → 用「忙碌」样式（0/0），别假装知道总数
        self.pmw_quick_progress.setRange(0, self.pmw_progress_total if self.pmw_progress_total else 0)
        self.pmw_quick_progress.setValue(0)
        self.pmw_quick_progress.setFormat(self.pmw_progress_text(0))
        self.pmw_quick_progress.show()
        if pms_message:
            self.pmw_status_message.setText(str(pms_message))

    def pmw_update_test_progress(self, pms_done: int, pms_detail: str = "") -> None:
        """任务进行中刷新进度条（单节点也会走到 0/1 → 1/1）。"""
        total = max(0, int(self.pmw_progress_total or 0))
        if total:
            done = max(0, min(int(pms_done or 0), total))
            self.pmw_quick_progress.setRange(0, total)
            self.pmw_quick_progress.setValue(done)
            self.pmw_quick_progress.setFormat(self.pmw_progress_text(done, pms_detail))
        else:
            self.pmw_quick_progress.setRange(0, 0)
            self.pmw_quick_progress.setFormat(self.pmw_progress_text(0, pms_detail))
        self.pmw_quick_progress.show()

    def pmw_end_test_progress(self) -> None:
        """任务收尾：还原进度条并（必要时）把速览条藏回设置要求的样子。"""
        self.pmw_quick_progress.setRange(0, 100)
        self.pmw_quick_progress.setValue(0)
        self.pmw_quick_progress.setFormat("%p%")
        self.pmw_quick_progress.hide()
        if self.pmw_progress_restore_quick:
            self.pmw_progress_restore_quick = False
            self.pmw_quick_frame.hide()
        self.pmw_progress_kind = ""
        self.pmw_progress_total = 0

    def pmw_notify_test_finished(self, pms_kind: str, pms_summary: str, pms_details: str = "", pms_dialog: bool = False) -> None:
        """测试结束后明确告诉用户「完成了没有」（v1.9.22）。

        用户反馈：测速跑完只有状态栏一闪而过的字，不知道到底成没成。
        这里按 v1.9.17「核心 / 内核更新完成」的同款做法：状态栏 + 托盘通知给一句话；
        `pms_dialog=True` 时再弹一个汇总框（用户手动发起的测速才弹，自动测试不打扰）。
        """
        stamp = pms_now_text("%H:%M:%S")
        text = f"{pms_summary}（{stamp}）"
        self.pmw_show_message(text)
        if self.pmw_tray is not None:
            self.pmw_tray.ptrc_notify(f"{PMS_APP_NAME} - {pms_kind}", pms_summary)
        if pms_dialog:
            box = QMessageBox(self)
            box.setIcon(QMessageBox.Icon.Information)
            box.setWindowTitle(f"{pms_kind}完成")
            box.setText(pms_summary)
            if pms_details:
                box.setInformativeText(pms_details)
            box.exec()

    def pmw_test_latency_selected(self) -> None:
        self.pmw_run_latency_test("selected", False)

    def pmw_test_latency_all(self) -> None:
        self.pmw_run_latency_test("all", False)

    def pmw_test_latency_page(self) -> None:
        """测试**当前页**的延迟（v1.7.0：分页后不必为了测一屏而拉全库）。"""
        self.pmw_run_latency_test("page", False)

    def pmw_test_speed_selected(self) -> None:
        self.pmw_run_speed_test("selected")

    def pmw_test_speed_all(self) -> None:
        """v1.9.10：测当前筛选结果的**全部**节点（跨页，不受分页限制）。"""
        self.pmw_run_speed_test("all")

    # ------------------------------------------------------------------ 「测新增的」（v1.9.61，用户 2026-10-15 需求）
    def pmw_test_untested_latency(self) -> None:
        """「仅测新增的」延迟（其余测试项同理）：在对应范围基础上裁掉已测过的。"""
        self.pmw_run_latency_test("all_untested", False)

    def pmw_test_untested_speed(self) -> None:
        self.pmw_run_speed_test("all_untested")

    def pmw_test_untested_telegram(self) -> None:
        self.pmw_run_telegram_test("all_untested")

    def pmw_test_untested_all(self) -> None:
        """一键跑三种（延迟 → 测速 → 电报，串行）。v1.9.65 起等价于弹窗里
        「范围=全部筛选结果 + 仅测新增 + 三项全勾」，实现走通用链 `pmw_test_chain_*`。"""
        self.pmw_start_test_chain("all", ("latency", "speed", "telegram"), pms_only_untested=True)

    # ------------------------------------------------------------------ 通用串行测试链（v1.9.65）
    def pmw_start_test_chain(self, pms_scope: str, pms_kinds: Sequence[str], pms_only_untested: bool = False) -> None:
        """按顺序串行跑多个测试项（延迟 → 测速 → 电报），供「选择测试」弹窗使用。

        用户定死顺序：延迟先跑（测出延迟 + 顺带 URL 模式的测速），再测速，最后电报。
        中断（stopped）时**不自动续下一段**——等用户手动决定，避免「以为跑完了其实后面没跑」。
        全部跑完才发一条汇总通知。
        """
        kinds = [kind for kind in ("latency", "speed", "telegram") if kind in tuple(pms_kinds or ())]
        if not kinds:
            return
        self.pmw_test_chain = [(kind, pms_scope) for kind in kinds]
        self.pmw_test_chain_total = len(self.pmw_test_chain)
        self.pmw_test_chain_done = 0
        self.pmw_test_chain_ran_any = False
        self.pmw_test_chain_stopped = False
        self.pmw_test_chain_only_untested = bool(pms_only_untested)
        # 与「仅测新增」勾选框保持同一开关：effective_scope 会按它补 _untested 后缀
        if self.pmw_test_chain_only_untested != bool(getattr(self, "pmw_only_untested", False)):
            self.pmw_only_untested = self.pmw_test_chain_only_untested
            action = self.pmw_actions.get("only_untested")
            if action is not None:
                action.setChecked(self.pmw_only_untested)
        self.pmw_test_chain_start_next()

    def pmw_test_chain_start_next(self) -> None:
        """跑测试链里的下一段；没有剩余就收尾。"""
        if not getattr(self, "pmw_test_chain", []):
            self.pmw_test_chain_finish()
            return
        kind, scope = self.pmw_test_chain.pop(0)
        self.pmw_test_chain_ran_any = True
        # 延迟 / 测速 / 电报 各自的 run 方法会接进度条与 finished 信号；
        # finished 里检测到链还在就调 pmw_test_chain_start_next() 续下一段。
        if kind == "speed":
            self.pmw_run_speed_test(scope)
        elif kind == "telegram":
            self.pmw_run_telegram_test(scope)
        else:  # latency / 兜底
            self.pmw_run_latency_test(scope, False)

    def pmw_test_chain_advance(self) -> None:
        """一段 finished 后调用：中断就不续，正常就跑下一段。"""
        self.pmw_test_chain_done += 1
        if getattr(self, "pmw_test_chain_stopped", False):
            self.pmw_test_chain_finish()
            return
        if not getattr(self, "pmw_test_chain", []):
            self.pmw_test_chain_finish()
            return
        self.pmw_test_chain_start_next()

    def pmw_test_chain_finish(self) -> None:
        """整条链跑完 / 中断：清状态、发汇总通知（只有真跑过才通知）。"""
        if not getattr(self, "pmw_test_chain_ran_any", False):
            return
        done = getattr(self, "pmw_test_chain_done", 0)
        total = getattr(self, "pmw_test_chain_total", 0)
        stopped = getattr(self, "pmw_test_chain_stopped", False)
        tail = "（已中断，后面的测试项未跑）" if stopped else ""
        self.pmw_notify_test_finished(
            "选择测试", f"「选择测试」完成 {done}/{total} 项{tail}",
            "延迟、测速、电报按勾选顺序依次跑完；已测过的不重测。")
        self.pmw_test_chain = []
        self.pmw_test_chain_ran_any = False
        self.pmw_test_chain_stopped = False

    def pmw_run_speed_test(self, pms_scope: str) -> None:
        """统一的测速入口：selected=选中 / page=本页 / all=全部筛选结果（跨页）。

        v1.9.10：范围会传给 `pst_start()` 写进断点，这样全量测速中断后
        点「继续上次测试」会按同一个范围重新取节点，剩下的仍能补回来。
        v1.9.61：勾了「仅测新增」时范围自动加 `_untested`（见 `pmw_effective_scope`）。
        """
        pms_scope = self.pmw_effective_scope(pms_scope)
        nodes = self.pmw_pick_test_nodes(pms_scope)
        if not nodes:
            # v1.9.65：串行链里某段无节点时，不能卡住——直接续下一段（或收尾）
            if getattr(self, "pmw_test_chain", []):
                self.pmw_test_chain_advance()
                return
            QMessageBox.information(self, "提示", "没有可测速的节点")
            return
        self.pmw_health_yield_to_manual()   # v1.9.37：手动测速优先，健康检查让路
        ok, message = self.pmw_speed.pst_start(nodes, pms_scope)
        self.pmw_show_message(message)
        if ok:
            self.pmw_begin_test_progress("speed", len(nodes), message)
            # v1.9.22：用户手动发起的测速才弹汇总框（自动测速不打扰），可在设置里关掉
            self.pmw_speed_dialog_wanted = True
        self.pmw_refresh_resume_action()

    # ------------------------------------------------------------------ 电报 DC 测试（v1.9.59）
    def pmw_test_telegram_selected(self) -> None:
        self.pmw_run_telegram_test("selected")

    def pmw_test_telegram_all(self) -> None:
        self.pmw_run_telegram_test("all")

    def pmw_run_telegram_test(self, pms_scope: str) -> None:
        """统一的电报 DC 测试入口（口径同测速：scope 写进断点，停后可续跑）。

        连通判定走 MTProto 握手（dcprobe.py），逐节点起临时核心；
        速度只有真拿到 HTTP 吞吐才显示，不做任何估算。
        v1.9.61：勾了「仅测新增」时范围自动加 `_untested`（见 `pmw_effective_scope`）。
        """
        pms_scope = self.pmw_effective_scope(pms_scope)
        nodes = self.pmw_pick_test_nodes(pms_scope)
        if not nodes:
            # v1.9.65：串行链里某段无节点时，不能卡住——直接续下一段（或收尾）
            if getattr(self, "pmw_test_chain", []):
                self.pmw_test_chain_advance()
                return
            QMessageBox.information(self, "提示", "没有可测试电报的节点")
            return
        self.pmw_health_yield_to_manual()   # 手动电报测试优先，健康检查让路
        ok, message = self.pmw_telegram.ptg_start(nodes, pms_scope)
        self.pmw_show_message(message)
        if ok:
            self.pmw_begin_test_progress("telegram_dc", len(nodes), message)
        self.pmw_refresh_resume_action()

    def pmw_on_telegram_progress(self, pms_done: int, pms_total: int, pms_name: str) -> None:
        self.pmw_progress_total = max(0, int(pms_total or 0))
        self.pmw_update_test_progress(pms_done, pms_name)
        self.pmw_status_message.setText(self.pmw_progress_text(pms_done, pms_name))

    def pmw_on_telegram_node_result(self, pms_uid: str, pms_result: dict) -> None:
        """单节点电报 DC 结果 → 写库 + 刷新（失败不把旧成绩抹成假数据：整组一起覆写）。"""
        node = self.pmw_node_by_uid(pms_uid)
        if node is None:
            return
        ok = bool(pms_result.get("ok"))
        try:
            best_ms = int(pms_result.get("best_ms", -1))
        except (TypeError, ValueError):
            best_ms = -1
        node.telegram_ok = 1 if ok else -1
        node.telegram_best_ms = best_ms if best_ms >= 0 else -1
        node.telegram_best_dc = str(pms_result.get("best_dc", "") or "")[:8]
        node.telegram_reachable = int(pms_result.get("proto_count", 0) or 0)
        node.telegram_tcp_count = int(pms_result.get("tcp_count", 0) or 0)
        node.telegram_total = int(pms_result.get("total", 0) or 0)
        node.telegram_speed_bps = float(pms_result.get("speed_bps", 0.0) or 0.0) if ok else 0.0
        node.telegram_time = pms_now_millis_text()
        node.telegram_note = str(pms_result.get("note", "") or "")[:500]
        self.pmw_repo.pnr_update_telegram_result(
            node.uid, ok, node.telegram_best_ms, node.telegram_best_dc,
            node.telegram_reachable, node.telegram_tcp_count, node.telegram_total,
            node.telegram_speed_bps, node.telegram_note,
        )
        self.pmw_node_model.pntm_update_node(node)

    def pmw_on_telegram_finished(self, pms_summary: dict) -> None:
        """电报测试结束：收进度条、给一句明白话（口径同 v1.9.22 测速收尾）。"""
        self.pmw_end_test_progress()
        pending = int(pms_summary.get("pending") or 0)
        total = int(pms_summary.get("total", 0) or 0)
        success = int(pms_summary.get("success", 0) or 0)
        best_ms = int(pms_summary.get("best_ms", -1) or -1)
        if pms_summary.get("stopped"):
            self.pmw_show_message(
                f"电报测试已中断：还剩 {pending} 个节点没测（点「继续上次测试」接着跑）" if pending
                else "电报测试已中断，这一轮已经测完了")
        else:
            tail = f"，最快往返 {best_ms} ms" if best_ms >= 0 else ""
            self.pmw_notify_test_finished(
                "电报测试", f"电报测试完成：可连 {success}/{total}{tail}",
                "连通判定以 MTProto 握手（req_pq_multi→resPQ）为准；"
                "只有真拿到 HTTP 吞吐才在「电报」列显示速度，逐 DC 明细见鼠标悬停提示。")
        self.pmw_refresh_resume_action()
        self.pmw_refresh_summary()
        # v1.9.65：串行测试链：电报是最后一段，结束即整条链收尾
        if getattr(self, "pmw_test_chain_ran_any", False):
            self.pmw_test_chain_stopped = bool(pms_summary.get("stopped"))
            self.pmw_test_chain_advance()
        self.pmw_emit_event("test.finished", {
            "kind": "telegram_dc",
            "success": success,
            "failed": int(pms_summary.get("failed", 0) or 0),
            "total": total,
            "stopped": bool(pms_summary.get("stopped", False)),
            "pending": pending,
        })


    def pmw_run_latency_test(self, pms_scope: str, pms_include_speed: bool) -> None:
        """统一的延迟测试入口。"""
        pms_scope = self.pmw_effective_scope(pms_scope)
        nodes = self.pmw_pick_test_nodes(pms_scope)
        if not nodes:
            # v1.9.65：串行链里某段无节点时，不能卡住——直接续下一段（或收尾）
            if getattr(self, "pmw_test_chain", []):
                self.pmw_test_chain_advance()
                return
            QMessageBox.information(self, "提示", "没有可测试的节点")
            return
        if self.pmw_settings.psm_get_str("test.mode", PMS_TEST_MODE_URL) == PMS_TEST_MODE_TCP:
            self.pmw_health_yield_to_manual()   # v1.9.37：手动测延迟优先
            self.pmw_run_tcp_latency_test(nodes)
            return
        self.pmw_health_yield_to_manual()   # v1.9.37：手动测延迟优先，健康检查让路
        ok, message = self.pmw_latency.plt_start(nodes, pms_include_speed, pms_scope)
        self.pmw_show_message(message)
        if ok:
            self.pmw_begin_test_progress("latency", len(nodes), message)
        self.pmw_refresh_resume_action()

    def pmw_resume_test_round(self, pms_round_id: str = "") -> None:
        """继续未完成的延迟测试 / 测速。

        v1.8.0：延迟与测速的断点共用一条 KV，所以先判断是哪一种，再按断点里记的 scope
        重新取一次节点——已测过的不会重测，中途新增的会自动补上。
        v1.9.54（审计第 19 项）：断点改成**多轮并存**，本方法多了 `pms_round_id`：
        不传 = 接着**最新一轮**跑（老行为不变）；传了 id 就精确续那一轮。
        """
        info = (self.pmw_latency.plt_pending_info() or self.pmw_speed.pst_pending_info()
                or self.pmw_telegram.ptg_pending_info())
        if str(pms_round_id or "").strip():
            # 点名了某一轮：从多轮清单里取它的 kind / scope（清单已按「旧 → 新」排好）
            picked = [item for item in self.pmw_test_round_list() if str(item.get("round_id")) == str(pms_round_id)]
            if not picked:
                self.pmw_show_message("这一轮断点已经不在了（可能已被丢弃或已跑完）")
                self.pmw_refresh_resume_action()
                return
            info = picked[0]
        if not info:
            QMessageBox.information(self, "提示", "没有未完成的测试，无需续跑。")
            self.pmw_refresh_resume_action()
            return
        pending = int(info.get("pending") or 0)
        total = int(info.get("total") or 0)
        round_kind = str(info.get("kind") or "")
        if round_kind == "latency":
            if self.pmw_latency.plt_is_running():
                self.pmw_show_message("已有延迟测试在跑，等它结束或停止后再续跑")
                return
            # TCP 模式不走批量调度器（没有断点概念），退化成按 scope 重跑一次
            if self.pmw_settings.psm_get_str("test.mode", PMS_TEST_MODE_URL) == PMS_TEST_MODE_TCP:
                self.pmw_run_latency_test(str(info.get("scope") or "all"), False)
                return
            ok, message = self.pmw_latency.plt_resume(
                self.pmw_pick_test_nodes(str(info.get("scope") or "all")), str(info.get("round_id") or ""))
        elif round_kind == "telegram_dc":
            # v1.9.59：电报 DC 断点续跑（口径同测速）
            if self.pmw_telegram.ptg_is_running():
                self.pmw_show_message("已有电报测试在跑，等它结束或停止后再续跑")
                return
            ok, message = self.pmw_telegram.ptg_resume(
                self.pmw_pick_test_nodes(str(info.get("scope") or "selected")), str(info.get("round_id") or ""))
        else:
            if self.pmw_speed.pst_is_running():
                self.pmw_show_message("已有测速在跑，等它结束或停止后再续跑")
                return
            ok, message = self.pmw_speed.pst_resume(
                self.pmw_pick_test_nodes(str(info.get("scope") or "selected")), str(info.get("round_id") or ""))
        self.pmw_show_message(message)
        if ok:
            self.pmw_begin_test_progress(round_kind if round_kind in ("latency", "telegram_dc") else "speed", total, message)
            self.pmw_update_test_progress(max(0, total - pending))
            if str(info.get("kind") or "") == "speed":
                self.pmw_speed_dialog_wanted = True
        self.pmw_refresh_resume_action()

    def pmw_test_round_list(self) -> list[dict]:
        """全部未完断点的清单（**旧 → 新**，末尾是最新一轮）。

        v1.9.54（审计第 19 项）：把两个调度器的断点合起来按创建时间排序。
        界面用它开「选择续哪一轮 / 丢哪一轮」的菜单（`pmw_open_resume_round_picker`），
        逻辑本身仍然全部在调度器里，主窗口只做接线。

        ★ v1.9.59：三个调度器的 store 扫的都是 `test.round.*` **全量前缀**，直接相加会
        把同一轮列三遍（菜单里点「续第 2 轮」会打到错误的轮次）。现在按 `round_id`
        去重——延迟 / 测速的 store 行为与 v1.9.54 完全一致（它们不设归属），只是在这里
        合并时把重复剔掉；电报 store 设了归属只吐电报轮次，天然是增量。
        """
        merged: dict[str, dict] = {}
        for item in (list(self.pmw_latency.plt_store._ptstore_list())
                     + list(self.pmw_speed.pst_store._ptstore_list())
                     + list(self.pmw_telegram.ptg_store._ptstore_list())):
            round_id = str(item.get("round_id") or "")
            if round_id and round_id not in merged:
                merged[round_id] = item
        items = list(merged.values())
        items.sort(key=lambda item: str(item.get("created_at") or ""))
        return items

    def pmw_clear_one_test_round(self, pms_round_id: str) -> None:
        """只丢点名的那一轮断点（**不碰其他轮**，v1.9.54）。"""
        target = str(pms_round_id or "").strip()
        if not target:
            return
        if str(target).startswith("speed:"):
            target = target.split(":", 1)[1]
        dropped = (self.pmw_latency.plt_clear_round(target) or self.pmw_speed.pst_clear_round(target)
                   or self.pmw_telegram.ptg_clear_round(target))   # v1.9.59：电报轮次也能点名丢
        self.pmw_refresh_resume_action()
        self.pmw_show_message("已丢弃这一轮断点" if dropped else "这一轮断点已经不在了")

    def pmw_open_resume_round_picker(self) -> None:
        """弹出「选择续跑哪一轮 / 丢哪一轮」的小菜单（v1.9.54，审计第 19 项）。

        为什么要它：断点不再只有一轮，主界面的「继续上次测试 / 丢弃断点」两个按钮
        默认只作用于**最新一轮**；这里给一个看得见全部轮次的入口，每一项都是真动作。
        """
        items = self.pmw_test_round_list()
        if not items:
            QMessageBox.information(self, "提示", "没有未完成的测试断点。")
            return
        menu = QMenu(self)
        for index, info in enumerate(items, 1):
            kind_text = self.pmw_round_kind_text(info.get("kind"))
            label = (f"{index}. {kind_text}：剩 {int(info.get('pending') or 0)}/{int(info.get('total') or 0)}"
                     f"（{info.get('created_at', '')}）")
            action = menu.addAction(label)
            action.triggered.connect(
                lambda _checked=False, rid=str(info.get("round_id") or ""): self.pmw_resume_test_round(rid))
        menu.addSeparator()
        for index, info in enumerate(items, 1):
            kind_text = self.pmw_round_kind_text(info.get("kind"))
            action = menu.addAction(f"丢弃第 {index} 轮（{kind_text}，{info.get('created_at', '')}）")
            action.triggered.connect(
                lambda _checked=False, rid=str(info.get("round_id") or ""): self.pmw_clear_one_test_round(rid))
        menu.exec(self.cursor().pos())

    def pmw_refresh_resume_action(self) -> None:
        """按当前断点刷新「继续上次测试」按钮的文案与可用状态。"""
        action = self.pmw_actions.get("test_resume")
        clear_action = self.pmw_actions.get("test_resume_clear")
        if action is None:
            return
        # v1.9.59：电报测试在跑 / 有电报断点也要反映到「继续上次测试」上
        running = self.pmw_latency.plt_is_running() or self.pmw_speed.pst_is_running() or self.pmw_telegram.ptg_is_running()
        info = {} if running else (self.pmw_latency.plt_pending_info() or self.pmw_speed.pst_pending_info()
                                   or self.pmw_telegram.ptg_pending_info())
        if clear_action is not None:
            clear_action.setEnabled(bool(info))
        base = str(action.property("pms_base_text") or "继续上次测试")
        if running:
            action.setText(base)
            action.setToolTip("测试正在跑，跑完或停止后才能续跑")
            action.setEnabled(False)
            return
        if not info:
            action.setText(base)
            action.setToolTip("没有未完成的测试")
            action.setEnabled(False)
            return
        kind_text = self.pmw_round_kind_text(info.get("kind"))
        pending = int(info.get("pending") or 0)
        action.setEnabled(True)
        action.setText(f"{base}（{kind_text}剩 {pending}）")
        action.setToolTip(
            f"上一次{kind_text}共 {int(info.get('total') or 0)} 个，还剩 {pending} 个没测（{info.get('created_at', '')} 开始）\n"
            "点这里接着跑：已测过的不重测，中途新增的节点会自动补上"
        )

    def pmw_clear_test_round(self) -> None:
        """丢掉断点（「不想续跑了」）。

        v1.9.54：这里仍然是「全丢」——精确丢某一轮走 `pmw_clear_one_test_round(轮次id)`，
        入口在 `pmw_open_resume_round_picker()` 的菜单里（主界面按钮不新增控件）。
        """
        self.pmw_latency.plt_clear_round()
        self.pmw_speed.pst_clear_round()
        self.pmw_telegram.ptg_clear_round()   # v1.9.59：电报断点一起丢（store 有归属，只清本类）
        self.pmw_refresh_resume_action()
        self.pmw_show_message("已丢弃未完成的测试断点")

    def pmw_run_tcp_latency_test(self, pms_nodes: Sequence[PmsProxyNode]) -> None:
        """TCP 握手延迟测试（多线程，逐个连接目标端口）。"""
        timeout_ms = self.pmw_settings.psm_get_int("test.tcp_timeout_ms", 3000)
        self.pmw_tcp_results: dict[str, int] = {}
        self.pmw_tcp_total = len(pms_nodes)
        self.pmw_tcp_done = 0
        self.pmw_tcp_signals = PmsTcpPingSignals(self)
        self.pmw_tcp_signals.ptcp_result.connect(self.pmw_on_tcp_ping_result)
        self.pmw_begin_test_progress("tcp", self.pmw_tcp_total,
                                     f"开始 TCP 延迟测试（{self.pmw_tcp_total} 个节点，超时 {timeout_ms} ms）")
        self.pmw_show_message(f"开始 TCP 延迟测试（{self.pmw_tcp_total} 个节点，超时 {timeout_ms} ms）")
        self.pmw_tcp_pool.clear()
        self.pmw_tcp_pool.setMaxThreadCount(max(1, self.pmw_settings.psm_get_int("test.concurrency", 8)))
        for node in pms_nodes:
            self.pmw_tcp_pool.start(PmsTcpPingTask(node, timeout_ms, self.pmw_tcp_signals))

    def pmw_on_tcp_ping_result(self, pms_uid: str, pms_latency: int, pms_error: str) -> None:
        """TCP 延迟测试结果回调（工作线程 → 主线程通过信号排队）。"""
        self.pmw_tcp_done += 1
        # v1.9.22：这里原来**只声明不写入**（`pmw_tcp_results` 是死字段），完成汇总拿不到成功数
        self.pmw_tcp_results[str(pms_uid)] = int(pms_latency)
        self.pmw_update_test_progress(self.pmw_tcp_done)
        node = self.pmw_node_by_uid(pms_uid)
        if node is None:
            return
        node.latency_ms = int(pms_latency) if pms_latency >= 0 else -1
        node.last_test_time = pms_now_millis_text()
        node.last_test_mode = "TCP"
        node.last_test_ok = 1 if pms_latency >= 0 else -1   # v1.9.8：失败要能在测速列看出来
        node.last_test_note = "" if pms_latency >= 0 else str(pms_error or "")   # v1.9.21
        if pms_latency >= 0:
            node.success_count += 1
        else:
            node.fail_count += 1
        self.pmw_repo.pnr_update_test_result(node.uid, node.latency_ms, node.speed_bps, "TCP", pms_latency >= 0, node.last_test_ok, node.last_test_note)
        self.pmw_repo.pnr_record_latency_history(node, "TCP", pms_error)
        self.pmw_node_model.pntm_update_node(node)
        self.pmw_label_latency.setText(f"延迟：{node.pnode_display_latency(self.pmw_show_core_fail())}")
        if self.pmw_tcp_done >= self.pmw_tcp_total:
            self.pmw_end_test_progress()
            self.pmw_reload_history_table()
            self.pmw_refresh_summary()
            ok_count = sum(1 for value in self.pmw_tcp_results.values() if value >= 0)
            self.pmw_notify_test_finished("TCP 延迟测试", f"TCP 延迟测试完成：成功 {ok_count}/{self.pmw_tcp_total}")

    def pmw_on_latency_progress(self, pms_done: int, pms_total: int, pms_name: str) -> None:
        if self.pmw_is_health_round():
            return   # v1.9.37：健康检查的进度走状态栏 / 托盘提示，不碰手动测试的进度条
        self.pmw_progress_total = max(0, int(pms_total or 0))
        self.pmw_update_test_progress(pms_done, pms_name)
        self.pmw_status_message.setText(self.pmw_progress_text(pms_done, pms_name))

    def pmw_on_latency_node_result(self, pms_uid: str, pms_latency: int, pms_speed: float, pms_success: bool, pms_note: str) -> None:
        """单节点延迟结果 → 写库 + 刷新界面。"""
        node = self.pmw_node_by_uid(pms_uid)
        if node is None:
            # v1.9.73 防御：内存里暂时没有该节点（极少见，如启动异步整库加载尚未落内存时
            # 恰好回结果、或节点被筛选移出内存但仍被测试）也先按 uid 落库，避免结果被静默丢弃。
            # 真实软件里首屏加载完成后才允许测试，本分支几乎不会命中；仅为堵住竞态风险。
            self.pmw_repo.pnr_update_test_result(
                str(pms_uid),
                int(pms_latency) if pms_latency >= 0 else None,
                0.0,
                "URL",
                pms_success,
                1 if pms_success else -1,
                str(pms_note or ""),
            )
            return
        node.latency_ms = int(pms_latency) if pms_latency >= 0 else -1
        node.last_test_time = pms_now_millis_text()
        node.last_test_mode = "URL"
        if pms_speed > 0:
            node.speed_bps = float(pms_speed)
        node.last_test_ok = 1 if pms_success else -1   # v1.9.8：失败不再把上次速度清零，但要在列里标出来
        node.last_test_note = "" if pms_success else str(pms_note or "")   # v1.9.21：留下核心原话
        if pms_success:
            node.success_count += 1
        else:
            node.fail_count += 1
        self.pmw_repo.pnr_update_test_result(node.uid, node.latency_ms, node.speed_bps, "URL", pms_success, node.last_test_ok, node.last_test_note)
        self.pmw_repo.pnr_record_latency_history(node, "URL", pms_note)
        self.pmw_node_model.pntm_update_node(node)
        self.pmw_label_latency.setText(f"延迟：{node.pnode_display_latency(self.pmw_show_core_fail())}")
        if node.uid == self.pmw_current_node_uid:
            self.pmw_set_current_node_label(
                f"当前节点：{node.pnode_display_name()}（延迟 {node.pnode_display_latency(self.pmw_show_core_fail())}）"
            )

    def pmw_on_latency_finished(self, pms_summary: dict) -> None:
        """测试结束 → 隐藏进度条、刷新历史、可选排序。

        v1.8.0：summary['stopped'] 为真表示被中断且断点已保留，提示语要说明还剩多少可续跑。
        v1.9.22：收尾统一走 `pmw_end_test_progress()`；完成时按 v1.9.17 的同款做法给
        **状态栏 + 托盘通知**（延迟测试不弹框——它常被用来排序，弹框会打断连续操作）。
        """
        self.pmw_end_test_progress()
        self.pmw_reload_history_table()
        if self.pmw_is_health_round():
            # v1.9.37：健康检查的收尾由服务自己做（连败/禁用/复活判定），界面只刷新一下表格
            self.pmw_refresh_summary()
            self.pmw_node_table.viewport().update()
            return
        if self.pmw_settings.psm_get_bool("test.sort_after_test", False):
            # v1.9.12：范围由 test.sort_scope_latency 决定（全量=排序后回第 1 页 / 当前页=只排本页）
            self.pmw_apply_auto_sort("latency")
        pending = int(pms_summary.get("pending") or 0)
        reasons = pms_summary.get("fail_reasons") or {}
        top = sorted(reasons.items(), key=lambda item: (-item[1], item[0]))[:3]
        detail = ("（" + "、".join(f"{name} {count}" for name, count in top) + "）") if top else ""
        if pms_summary.get("stopped"):
            self.pmw_status_message.setText(
                f"测试已中断：还剩 {pending} 个节点没测（点「继续上次测试」接着跑）" if pending else "测试已中断，这一轮已经测完了"
            )
        else:
            self.pmw_status_message.setText(
                f"测试完成：成功 {pms_summary.get('success', 0)} / 失败 {pms_summary.get('failed', 0)}{detail}"
            )
            self.pmw_notify_test_finished(
                "延迟测试",
                f"延迟测试完成：成功 {pms_summary.get('success', 0)}/{pms_summary.get('total', 0)}"
                + (f"，失败 {pms_summary.get('failed', 0)}{detail}" if pms_summary.get("failed") else "，全部成功"),
            )
        # v1.9.21：补一次数量统计——「已测出延迟 N」原来在测试结束后不刷新，
        # 明明测出一堆延迟却还显示 0，看着像测试没生效。
        self.pmw_refresh_summary()
        self.pmw_stats_labels["last_test"].setText(pms_now_text())
        self.pmw_settings.psm_set("test.last_run_time", pms_now_text(), pms_emit=False)
        self.pmw_refresh_resume_action()
        # v1.9.65：串行测试链：延迟段结束后续下一段（测速）
        if getattr(self, "pmw_test_chain_ran_any", False):
            self.pmw_test_chain_stopped = bool(pms_summary.get("stopped"))
            self.pmw_test_chain_advance()
        self.pmw_emit_event("test.finished", {
            "kind": "latency",
            "success": int(pms_summary.get("success", 0) or 0),
            "failed": int(pms_summary.get("failed", 0) or 0),
            "total": int(pms_summary.get("total", 0) or 0),
            "stopped": bool(pms_summary.get("stopped", False)),
            "pending": pending,
        })

    def pmw_on_speed_progress(self, pms_done: int, pms_total: int, pms_name: str, pms_bytes: int) -> None:
        self.pmw_progress_total = max(0, int(pms_total or 0))
        detail = f"{pms_name}（已下载 {pms_format_bytes(pms_bytes)}）" if pms_bytes else pms_name
        self.pmw_update_test_progress(pms_done, detail)
        self.pmw_status_message.setText(self.pmw_progress_text(pms_done, detail))

    def pmw_on_speed_node_result(self, pms_uid: str, pms_speed: float, pms_latency_ms: int, pms_success: bool, pms_note: str) -> None:
        """单节点测速结果 → 写库 + 刷新。

        v1.9.8 修三处：
        1. 第三参其实是**延迟毫秒**（调度器按 uid / 速度 / 延迟 / 成败 / 说明的顺序发信号），
           之前被当成「已下载字节数」拿去 `pms_format_bytes()`，历史详情里会写出「243 B」这种假数据；
        2. 失败时不再把 `speed_bps` 写成 0 —— 那会把上一次测出的成绩抹掉，列里只剩「-」；
        3. 失败用 `last_test_ok=-1` 标记，测速列显示「失败」，跟「没测过」区分开。
        """
        node = self.pmw_node_by_uid(pms_uid)
        if node is None:
            return
        ok = bool(pms_success) and float(pms_speed or 0.0) > 0
        if ok:
            node.speed_bps = float(pms_speed)
        node.last_test_time = pms_now_millis_text()
        node.last_test_mode = "测速"
        node.last_test_ok = 1 if ok else -1
        if ok:
            node.success_count += 1
        else:
            node.fail_count += 1
        self.pmw_repo.pnr_update_test_result(node.uid, node.latency_ms, node.speed_bps, "测速", ok, node.last_test_ok, "" if ok else str(pms_note or ""))
        detail = str(pms_note or "")
        if not detail:
            detail = f"耗时 {int(pms_latency_ms)} ms" if int(pms_latency_ms or 0) > 0 else "测速完成"
        self.pmw_repo.pnr_record_latency_history(node, "测速", detail)
        self.pmw_node_model.pntm_update_node(node)
        self.pmw_label_speed.setText(f"速度：{node.pnode_display_speed()}")

    def pmw_on_speed_finished(self, pms_summary: dict) -> None:
        """测速结束 → 收进度条、刷新、**明确告诉用户跑完了**（v1.9.22）。

        用户反馈：测速跑完只有状态栏一闪而过的字，不知道成没成。
        现在：状态栏 + 托盘通知 +（用户手动发起且没关掉设置时）一个汇总框。
        """
        self.pmw_end_test_progress()
        self.pmw_reload_history_table()
        # v1.9.33：测速结束后的「自动切到最快节点」（与定时自动切换共用同一条切换链路；
        # 服务内部自己看开关 / 范围 / 核心是否在跑，主窗口只负责触发）
        self.pmw_autoswitch.pasw_on_speedtest_finished(pms_summary)
        pending = int(pms_summary.get("pending") or 0)
        # v1.9.12：测速结束后可选自动排序（速度高的排前面），开关与范围独立于延迟测试
        if self.pmw_settings.psm_get_bool("test.sort_after_speed", False):
            self.pmw_apply_auto_sort("speed")
        want_dialog = bool(getattr(self, "pmw_speed_dialog_wanted", False)) and self.pmw_settings.psm_get_bool(
            "test.speed_result_dialog", True
        )
        self.pmw_speed_dialog_wanted = False
        if pms_summary.get("stopped"):
            self.pmw_show_message(
                f"测速已中断：还剩 {pending} 个节点没测（点「继续上次测试」接着跑）" if pending else "测速已中断，这一轮已经测完了"
            )
        else:
            total = int(pms_summary.get("total", 0) or 0)
            success = int(pms_summary.get("success", 0) or 0)
            best = pms_format_speed(pms_summary.get("best_speed", 0.0))
            # v1.9.8：全部失败时给一句能看懂的话，否则「成功 0/91，最快 0 B/s」看着像程序坏了
            if total and success == 0:
                summary = f"测速完成：{total} 个节点全部失败"
                details = (
                    "节点连不上测速源，多半已失效或测速源被墙。\n"
                    "测速列会显示「失败」，失败原因见鼠标悬停提示与「最近 20 次延迟测试记录」的「说明」列。"
                )
            else:
                summary = f"测速完成：成功 {success}/{total}，最快 {best}"
                failed = int(pms_summary.get("failed", 0) or 0)
                details = (
                    f"最快 {best}；成功 {success} 个" + (f"，失败 {failed} 个" if failed else "，全部成功") + "。\n"
                    "结果已写入节点表（测速列），失败原因见鼠标悬停提示与「最近 20 次延迟测试记录」的「说明」列。"
                )
            self.pmw_notify_test_finished("测速", summary, details, pms_dialog=want_dialog)
        self.pmw_refresh_resume_action()
        # v1.9.65：串行测试链：测速段结束后续下一段（电报）
        if getattr(self, "pmw_test_chain_ran_any", False):
            self.pmw_test_chain_stopped = bool(pms_summary.get("stopped"))
            self.pmw_test_chain_advance()
        # v1.9.21：测速结束同样补一次数量统计
        self.pmw_refresh_summary()
        self.pmw_emit_event("test.finished", {
            "kind": "speed",
            "success": int(pms_summary.get("success", 0) or 0),
            "failed": int(pms_summary.get("failed", 0) or 0),
            "total": int(pms_summary.get("total", 0) or 0),
            "stopped": bool(pms_summary.get("stopped", False)),
            "pending": pending,
        })

    # ------------------------------------------------------------------ 选择测试弹窗（v1.9.65）
    def pmw_open_test_picker(self) -> None:
        """开「选择测试」弹窗：范围单选（选中 / 本页 / 全部筛选 / 续测）+ 测试项多选。

        v1.9.65 起右键「测试 ▸」与顶部「测试 ▾」里散落的十来个测试入口收敛成这一个弹窗
        （用户 2026-10-15：改成直接弹窗，选范围、多勾项目、按勾选的去测）。条数预览直接调
        既有的三个取节点方法，保证与真正开跑时取到的节点一致。
        """
        from pms.gui.dialogs.testpickerdialog import PmsTestPickerDialog
        dialog = PmsTestPickerDialog(
            pms_selected_count=len(self.pmw_selected_nodes()),
            pms_page_count=len(self.pmw_page_nodes()),
            pms_all_count=len(self.pmw_visible_nodes()),
            pms_only_untested=bool(getattr(self, "pmw_only_untested", False)),
            pms_has_pending=bool(self.pmw_test_round_list()),
            pms_parent=self,
        )
        dialog.tpd_run_requested.connect(self.pmw_on_test_picker_run)
        dialog.exec()

    def pmw_on_test_picker_run(self, pms_scope: str, pms_kinds: list, pms_only_untested: bool) -> None:
        """弹窗确认后的唯一分发口：续测走断点，其余按勾选项起串行链。"""
        scope = str(pms_scope or "")
        if scope == "resume":
            self.pmw_resume_test_round("")
            return
        kinds = [str(kind) for kind in (pms_kinds or []) if str(kind) in ("latency", "speed", "telegram")]
        if scope not in ("selected", "page", "all") or not kinds:
            return
        self.pmw_start_test_chain(scope, kinds, pms_only_untested)

    # ------------------------------------------------------------------ 电报代理连通直测（v1.9.68）
    def pmw_open_tg_proxy_test(self) -> None:
        """开「电报代理连通直测」弹窗：粘贴 t.me/proxy 链接逐条原生 socket 探。

        MTProto 代理不能当通用出口（Xray/sing-box 无 MTProto 出站），走不了节点表里的
        延迟/测速/电报 DC 测试（那些经临时核心隧道）。这里直连代理本身做 MTProto 握手，
        能拿到 resPQ 即证明代理在线且能连通电报 DC。结果只展示不落库（不进路由）。

        v1.9.76 方案 B：从 TgProxy / TgWebProxy 标签页唤起时，把**当前选中**（没有选中则
        当前页可见）的 t.me 链接预填进弹窗，省掉手动粘贴。
        """
        from pms.gui.dialogs.tgproxytestdialog import PmsTgProxyTestDialog
        initial_links = ""
        try:
            nodes = self.pmw_selected_nodes() or self.pmw_page_nodes()
            links = self.pmwt_tg_links_of_nodes(nodes)
            if links:
                initial_links = "\n".join(links)
        except Exception:  # noqa: BLE001 - 预填是增强项，失败也要能开弹窗
            initial_links = ""
        dialog = PmsTgProxyTestDialog(pms_parent=self, pms_initial_links=initial_links)
        dialog.exec()
