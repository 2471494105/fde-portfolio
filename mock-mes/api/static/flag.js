/*
 * 看板与业务两套状态的「事实对不上」判定——四个写点共用这一份实现。
 *
 * 为什么单独拆出来：同一条规则原来在四个地方各写一遍——
 *   1) index.html 的 VIEWS.work_orders.flag（行标红/橙）
 *   2) index.html 的 refreshKpi（KPI 计数）
 *   3) scripts/verify_ui.py 的 expect_wo（UI 期望值）
 *   4) fde-diagnosis-agent 的 app/ds/checks.py（治理判据）
 * 前两个连词都硬写，"已完成"/"已关闭"改一处漏一处不会有人发现；第三份是 Python，
 * 与第四份是同一规则的两个实现，谁改了谁都不会响。所以把 1、2 合成这里一份，
 * 再由 fde-diagnosis-agent 的判据 H 拿它当 counterpart 做「同一批工单、两侧必须同名」的对账。
 *
 * 词不写死在函数里：词是客户的叫法，由 MES_WORDS 提供（判据 H 会核对它与
 * status_policy.json 里 client_a 的 closed_order_labels / stage_board_closed 逐字一致）。
 */
(function (root) {
  "use strict";

  // 客户端看板看到的词。改这里要同时改 status_policy.json，否则判据 H 立刻红。
  // 「已合并」不在里面：合并后看板跟着合并到的那张单走，拿它判这张单是把别人的看板算到它头上。
  var MES_WORDS = {
    closedOrderLabels: ["已完成", "已取消"],  // 业务状态里哪些词代表「单已关」
    boardClosed: ["已关闭"]                  // 看板阶段里哪个词代表「看板已关」
  };

  function pick(w) {
    var src = w || {};
    return {
      closedOrderLabels: (src.closedOrderLabels || MES_WORDS.closedOrderLabels).slice(),
      boardClosed: (src.boardClosed || MES_WORDS.boardClosed).slice()
    };
  }

  // 两套状态并存，谁也不是权威，只能判「事实与状态对不上」，两个方向分开报：
  // 红 = 业务说关单了但看板还挂在别处；橙 = 看板已关闭却没有关单时间。
  function rowFlag(r, w) {
    var W = pick(w);
    // 看板阶段为空 = 没有看板可对，不算冲突（判据侧同样跳过；空值不是「没关」的证据）
    if (r.stage_id && W.closedOrderLabels.indexOf(r.state) >= 0 &&
        W.boardClosed.indexOf(r.stage_id) < 0) {
      return { cls: "row-red", tags: [["关单未关板", "tag-red"]] };
    }
    if (W.boardClosed.indexOf(r.stage_id) >= 0 && !r.closed_at) {
      return { cls: "row-amber", tags: [["关板未关单", "tag-amber"]] };
    }
    return { cls: "", tags: [] };
  }

  function counts(rows, w) {
    var red = 0, amber = 0;
    (rows || []).forEach(function (r) {
      var f = rowFlag(r, w);
      if (f.cls === "row-red") red++;
      else if (f.cls === "row-amber") amber++;
    });
    return { red: red, amber: amber };
  }

  // 给判据 H 用：同一批行，按工单号列出两侧判定，方便逐号对账（不比总数，总数相等可能是一换一）
  function judge(rows, w) {
    var red = [], amber = [];
    (rows || []).forEach(function (r) {
      var f = rowFlag(r, w);
      if (f.cls === "row-red") red.push(r.wo_no);
      else if (f.cls === "row-amber") amber.push(r.wo_no);
    });
    return { red: red, amber: amber };
  }

  var api = { rowFlag: rowFlag, counts: counts, judge: judge, WORDS: MES_WORDS };
  if (typeof module !== "undefined" && module.exports) { module.exports = api; }
  root.MesFlag = api;
})(typeof globalThis !== "undefined" ? globalThis : this);
