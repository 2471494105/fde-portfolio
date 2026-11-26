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

  // 缺陷表的行级规则。阈值不写死在比较里：它是「口径」，与 data/mapping/field_rules.json
  // 的 min/max 必须一致（判据 K 会拿 node 读这里的默认值去核对）。
  var DEFECT_LIMITS = {
    confMin: 0,
    confMax: 100
  };

  // 三类脏，各自一个标签、各自的优先级：红 > 橙 > 灰（一行可以同时踩两条）。
  // now 由调用方注入（页面用 nowText()，对账时喂同一个字面时刻）——两个实现各自取钟，
  // 边界行就会随机对不上。
  function defectRowFlag(r, o) {
    var opt = o || {};
    var L = {
      confMin: (opt.limits && opt.limits.confMin !== undefined) ? opt.limits.confMin : DEFECT_LIMITS.confMin,
      confMax: (opt.limits && opt.limits.confMax !== undefined) ? opt.limits.confMax : DEFECT_LIMITS.confMax
    };
    var now = opt.now || "";
    var tags = [], kinds = [], cls = "";
    var v = r.conf_pct;
    if (v !== null && v !== undefined && (Number(v) > L.confMax || Number(v) < L.confMin)) {
      tags.push(["超范围", "tag-red"]); kinds.push("conf"); cls = "row-red";
    }
    if (r.captured_at && now && String(r.captured_at) > now) {
      tags.push(["未来时间", "tag-amber"]); kinds.push("future");
      if (cls !== "row-red") cls = "row-amber";
    }
    if (r.lot_no === null || r.lot_no === "") {
      tags.push([r.lot_no === null ? "批次 NULL" : "批次空串", "tag-gray"]);
      kinds.push(r.lot_no === null ? "lot_null" : "lot_empty");
      if (!cls) cls = "row-gray";
    }
    return { cls: cls, tags: tags, kinds: kinds };
  }

  // 给判据 K 用：同一批行按编号列出每一类，两侧逐号对账（不比条数——一条行可踩两条）
  function defectJudge(rows, o) {
    var out = { conf: [], future: [], lot_null: [], lot_empty: [], violating: [] };
    (rows || []).forEach(function (r) {
      var f = defectRowFlag(r, o);
      if (!f.kinds.length) return;
      var id = String(r.rec_id);
      out.violating.push(id);
      f.kinds.forEach(function (k) { out[k].push(id); });
    });
    return out;
  }

  function defectCounts(rows, o) {
    var j = defectJudge(rows, o);
    return {
      violating: j.violating.length,
      conf: j.conf.length,
      future: j.future.length,
      lot_null: j.lot_null.length,
      lot_empty: j.lot_empty.length
    };
  }

  var api = { rowFlag: rowFlag, counts: counts, judge: judge, WORDS: MES_WORDS,
              defectRowFlag: defectRowFlag, defectJudge: defectJudge,
              defectCounts: defectCounts, DEFECT_LIMITS: DEFECT_LIMITS };
  if (typeof module !== "undefined" && module.exports) { module.exports = api; }
  root.MesFlag = api;
})(typeof globalThis !== "undefined" ? globalThis : this);
