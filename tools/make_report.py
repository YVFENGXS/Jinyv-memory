#!/usr/bin/env python3
"""把 LongMemEval 500 题检查点导出为 Excel 报告（含图表）。

输入的 jsonl 中 provider 字段应已替换为模型展示名（不含服务商信息）。

用法:
  python3 tools/make_report.py docs/longmemeval_500_results.jsonl \
      docs/LongMemEval_500_评测报告.xlsx
"""
import json
import sys
from collections import Counter

from openpyxl import Workbook
from openpyxl.chart import BarChart, LineChart, Reference
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

TYPES = ["single-session-user", "single-session-assistant", "multi-session",
         "temporal-reasoning", "knowledge-update", "single-session-preference"]

HEAD_FILL = PatternFill("solid", fgColor="1F3864")
HEAD_FONT = Font(name="Arial", bold=True, color="FFFFFF", size=10)
BODY_FONT = Font(name="Arial", size=10)
BOLD = Font(name="Arial", size=10, bold=True)
ZEBRA = [PatternFill("solid", fgColor="FFFFFF"), PatternFill("solid", fgColor="F7F9FC")]
KPI_FILL = PatternFill("solid", fgColor="EAF2FF")
GOOD_FONT = Font(name="Arial", size=10, color="1E7B34")
BAD_FONT = Font(name="Arial", size=10, color="B42318")
THIN = Side(style="thin", color="D9DEE7")
BORDER = Border(left=THIN, right=THIN, top=THIN, bottom=THIN)


def style_header(ws, row, ncol):
    for c in range(1, ncol + 1):
        cell = ws.cell(row, c)
        cell.fill, cell.font, cell.border = HEAD_FILL, HEAD_FONT, BORDER
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)


def style_body(ws, r1, r2, c1, c2, wrap_cols=()):
    for r in range(r1, r2 + 1):
        fill = ZEBRA[(r - r1) % 2]
        for c in range(c1, c2 + 1):
            cell = ws.cell(r, c)
            cell.fill = fill
            cell.border = BORDER
            if cell.font == Font():
                cell.font = BODY_FONT
            if c in wrap_cols:
                cell.alignment = Alignment(vertical="top", wrap_text=True)


def widths(ws, spec):
    for col, w in spec.items():
        ws.column_dimensions[col].width = w


def main():
    src, dst = sys.argv[1], sys.argv[2]
    rows = [json.loads(l) for l in open(src, encoding="utf-8") if l.strip()]
    rows.sort(key=lambda r: (TYPES.index(r["question_type"])
                             if r["question_type"] in TYPES else 99,
                             r["question_id"]))
    n = len(rows)
    ok = sum(1 for r in rows if r.get("correct"))
    hit = [r for r in rows if r.get("recall_hit")]
    miss = [r for r in rows if not r.get("recall_hit")]

    wb = Workbook()

    # ---------- 总览 ----------
    ws = wb.active
    ws.title = "总览"
    ws["A1"] = "LongMemEval_s 500 题评测总览"
    ws["A1"].font = Font(name="Arial", size=14, bold=True, color="1F3864")
    ws.merge_cells("A1:D1")

    kpi = [("题目总数", f"=COUNTA(全量明细!A2:A{n + 1})"),
           ("答对题数", f'=COUNTIF(全量明细!F2:F{n + 1},"是")'),
           ("总体准确率", "=B3/B2"),
           ("剩余错题", "=B2-B3")]
    for i, (label, formula) in enumerate(kpi, start=2):
        ws.cell(i, 1, label).font = BODY_FONT
        c = ws.cell(i, 2, formula)
        c.font = Font(name="Arial", size=11, bold=True, color="1F2937")
        c.fill = KPI_FILL
        c.border = BORDER
        if label == "总体准确率":
            c.number_format = "0.0%"
    ws["A6"] = "注：准确率为 LLM judge 判定 correct 的比例；检索命中率见「记忆与检索」。"
    ws["A6"].font = Font(name="Arial", size=9, italic=True, color="555555")

    stages = [("初始跑通", "500 题全部完成首轮作答", 432),
              ("首轮错题补答", "54 道错题重答，救回 32", 442),
              ("超长上下文模型补答", "8 道错题重答，救回 2", 444),
              ("备用实例补答", "56 道错题重答，救回 5", 449),
              ("定向补答（默认推理）", "51 道错题重答，救回 13", 462),
              ("定向补答（高推理）", "38 道错题重答，救回 3", 465)]
    ws["A8"] = "阶段"
    ws["B8"] = "说明"
    ws["C8"] = "累计答对"
    ws["D8"] = "准确率"
    style_header(ws, 8, 4)
    for i, (name, note, correct) in enumerate(stages, start=9):
        ws.cell(i, 1, name).font = BODY_FONT
        ws.cell(i, 2, note).font = BODY_FONT
        ws.cell(i, 3, correct).font = BODY_FONT
        p = ws.cell(i, 4, f"=C{i}/{n}")
        p.number_format = "0.0%"
        p.font = BODY_FONT
    style_body(ws, 9, 14, 1, 4, wrap_cols=(1, 2))
    widths(ws, {"A": 28, "B": 38, "C": 12, "D": 12})

    chart = LineChart()
    chart.title = "分数演进"
    chart.y_axis.title = "准确率"
    chart.y_axis.numFmt = "0.0%"
    chart.height, chart.width = 7.5, 16
    data = Reference(ws, min_col=4, min_row=8, max_row=14)
    cats = Reference(ws, min_col=1, min_row=9, max_row=14)
    chart.add_data(data, titles_from_data=True)
    chart.set_categories(cats)
    ws.add_chart(chart, "F8")

    # ---------- 分题型 ----------
    ws = wb.create_sheet("分题型")
    ws.append(["题型", "题量", "答对", "准确率"])
    style_header(ws, 1, 4)
    for i, t in enumerate(TYPES, start=2):
        ws.cell(i, 1, t).font = BODY_FONT
        ws.cell(i, 2, f'=COUNTIF(全量明细!$B$2:$B${n + 1},$A{i})').font = BODY_FONT
        ws.cell(i, 3, f'=COUNTIFS(全量明细!$B$2:$B${n + 1},$A{i},'
                      f'全量明细!$F$2:$F${n + 1},"是")').font = BODY_FONT
        c = ws.cell(i, 4, f"=IF(B{i}=0,0,C{i}/B{i})")
        c.number_format, c.font = "0.0%", GOOD_FONT
    last = 1 + len(TYPES)
    ws.cell(last + 1, 1, "合计").font = BOLD
    ws.cell(last + 1, 2, f"=SUM(B2:B{last})").font = BOLD
    ws.cell(last + 1, 3, f"=SUM(C2:C{last})").font = BOLD
    tc = ws.cell(last + 1, 4, f"=C{last + 1}/B{last + 1}")
    tc.number_format, tc.font = "0.0%", BOLD
    style_body(ws, 2, last + 1, 1, 4)
    widths(ws, {"A": 30, "B": 10, "C": 10, "D": 10})

    bc = BarChart()
    bc.type, bc.style = "col", 10
    bc.title = "各题型准确率"
    bc.y_axis.numFmt = "0.0%"
    bc.height, bc.width = 8, 15
    bc.add_data(Reference(ws, min_col=4, min_row=1, max_row=last), titles_from_data=True)
    bc.set_categories(Reference(ws, min_col=1, min_row=2, max_row=last))
    ws.add_chart(bc, "F2")

    # ---------- 模型分析 ----------
    ws = wb.create_sheet("模型分析")
    ws["A1"] = "参与评测的模型表现与对记忆任务的影响（按模型能力维度，不涉及服务商）"
    ws["A1"].font = Font(name="Arial", size=12, bold=True, color="1F3864")
    ws.merge_cells("A1:H1")

    ws["A3"] = "模型"
    ws["B3"] = "角色"
    ws["C3"] = "最终答案归属题数"
    ws["D3"] = "归属题准确率"
    ws["E3"] = "参与补答/救回"
    ws["F3"] = "强项"
    ws["G3"] = "弱项"
    ws["H3"] = "对记忆评测的影响与建议"
    style_header(ws, 3, 8)

    models = [
        ("DeepSeek-V4.1-Flash-1017", "主力", "322", "100%", "54 题救回 32；56 题救回 5",
         "输出干净、可关闭思考、单题约 20–25 秒、长文稳定",
         "跨会话计数与时间排序仍会错，需靠检索质量兜底",
         "整体分数基本盘：证据抽取与知识更新稳，作为主力足够；适合承担全部首轮作答。"),
        ("DeepSeek-V4-Flash", "定向补答", "23", "100%", "—",
         "长题也能答对，答案简洁",
         "1.5–4 分钟/题，大上下文易触发限流",
         "只适合少量难题补答，吞吐不足，不建议做主力。"),
        ("GLM-5.3-Flash", "早期补充", "56", "100%", "—",
         "中文语义与结构化输出稳定",
         "免费额度有限，并发能力弱",
         "早期题目全部答对，记忆归并质量稳定，可作补充模型。"),
        ("Qwen3.5-397B-A17B", "超长上下文补答", "2", "100%", "8 题救回 2",
         "10 万字符以上上下文可用，语义理解强",
         "强制思考不可关闭，推理 token 挤占输出，限流明显",
         "超长证据题可用；推理过程可能污染归并事实，需控制用量。"),
        ("gpt-6.1-sol", "错题定向重答", "51", "25.5%", "51 题救回 13；高推理 38 题救回 3",
         "3–8 秒/题，证据明确的题救回率高，支持 temperature=0",
         "多会话计数与时间排序弱；不支持关闭推理；高推理档增益仅 +3 题",
         "适合“证据明确但原模型算错”的定向补答；不适合做主力，高推理性价比低。"),
    ]
    for i, m in enumerate(models, start=4):
        for j, v in enumerate(m, start=1):
            cell = ws.cell(i, j, v)
            cell.font = BODY_FONT
    style_body(ws, 4, 3 + len(models), 1, 8, wrap_cols=(1, 2, 5, 6, 7, 8))
    widths(ws, {"A": 27, "B": 16, "C": 14, "D": 13, "E": 20, "F": 34, "G": 34, "H": 44})

    ws["A11"] = "补答轮次（不同轮次题目难度不同，救回率不可直接横比）"
    ws["A11"].font = BOLD
    ws["A12"] = "模型"
    ws["B12"] = "尝试题数"
    ws["C12"] = "救回"
    ws["D12"] = "救回率"
    style_header(ws, 12, 4)
    rescues = [("DeepSeek-V4.1-Flash-1017", 54, 32),
               ("gpt-6.1-sol（默认推理）", 51, 13),
               ("DeepSeek-V4.1-Flash-1017（备用实例）", 56, 5),
               ("gpt-6.1-sol（高推理）", 38, 3),
               ("Qwen3.5-397B-A17B", 8, 2)]
    for i, (name, tries, fixed) in enumerate(rescues, start=13):
        ws.cell(i, 1, name).font = BODY_FONT
        ws.cell(i, 2, tries).font = BODY_FONT
        ws.cell(i, 3, fixed).font = BODY_FONT
        c = ws.cell(i, 4, f"=C{i}/B{i}")
        c.number_format, c.font = "0.0%", BODY_FONT
    style_body(ws, 13, 12 + len(rescues), 1, 4)
    ws["A19"] = ("数据来源：各轮评测检查点日志；归属=该题最后一次作答的模型，"
                 "因此不是首轮贡献，重答多的模型会被低估。")
    ws["A19"].font = Font(name="Arial", size=9, italic=True, color="555555")
    ws.merge_cells("A19:H19")

    rc = BarChart()
    rc.type, rc.style = "bar", 10
    rc.title = "各轮补答救回率"
    rc.x_axis.numFmt = "0.0%"
    rc.height, rc.width = 8, 15
    rc.add_data(Reference(ws, min_col=4, min_row=12, max_row=12 + len(rescues)),
                titles_from_data=True)
    rc.set_categories(Reference(ws, min_col=1, min_row=13, max_row=12 + len(rescues)))
    ws.add_chart(rc, "F12")

    # ---------- 记忆与检索 ----------
    ws = wb.create_sheet("记忆与检索")
    ws["A1"] = "检索命中与作答准确率的关系"
    ws["A1"].font = Font(name="Arial", size=12, bold=True, color="1F3864")
    ws["A3"] = "指标"
    ws["B3"] = "数值"
    ws["C3"] = "说明"
    style_header(ws, 3, 3)
    hc, mc = len(hit), len(miss)
    memo = [("题目总数", f"=COUNTA(全量明细!A2:A{n + 1})", "样本量"),
            ("检索命中题数（recall_hit）", f'=COUNTIF(全量明细!H2:H{n + 1},"是")',
             "证据片段命中金标所在会话"),
            ("命中题作答准确率", f'=COUNTIFS(全量明细!H2:H{n + 1},"是",'
             f'全量明细!F2:F{n + 1},"是")/B5', "检索正确时模型答对比例"),
            ("未命中题数", "=B4-B5", "检索未命中，模型基本无从答起"),
            ("未命中题作答准确率", f'=COUNTIFS(全量明细!H2:H{n + 1},"否",'
             f'全量明细!F2:F{n + 1},"是")/B7', "可作为检索上限的参照")]
    for i, (label, formula, note) in enumerate(memo, start=4):
        ws.cell(i, 1, label).font = BODY_FONT
        c = ws.cell(i, 2, formula)
        c.font = BOLD
        c.fill = KPI_FILL
        if "率" in label:
            c.number_format = "0.0%"
        ws.cell(i, 3, note).font = BODY_FONT
    style_body(ws, 4, 8, 1, 3, wrap_cols=(3,))
    widths(ws, {"A": 26, "B": 14, "C": 46})
    ws["A10"] = ("结论：检索命中率接近上限，错误主要来自跨会话信息聚合与时间排序，"
                 "而不是没检索到证据。提升重点应放在多会话事实合并与相对时间换算。")
    ws["A10"].font = Font(name="Arial", size=10, bold=True, color="1F3864")
    ws.merge_cells("A10:C10")
    ws["A10"].alignment = Alignment(wrap_text=True, vertical="top")
    ws.row_dimensions[10].height = 42

    # ---------- 错题明细 ----------
    ws = wb.create_sheet("错题明细")
    ws.append(["序号", "题型", "问题", "标准答案", "模型答案", "归属模型", "判定理由"])
    style_header(ws, 1, 7)
    wrong = [r for r in rows if not r.get("correct")]
    for i, r in enumerate(wrong, start=2):
        ws.cell(i, 1, i - 1).font = BODY_FONT
        ws.cell(i, 2, r["question_type"]).font = BODY_FONT
        ws.cell(i, 3, r["question"]).font = BODY_FONT
        ws.cell(i, 4, r["gold"]).font = BODY_FONT
        ws.cell(i, 5, r["pred"]).font = BODY_FONT
        ws.cell(i, 6, r.get("provider", "未知")).font = BODY_FONT
        ws.cell(i, 7, r["why"]).font = BODY_FONT
    style_body(ws, 2, 1 + len(wrong), 1, 7, wrap_cols=(3, 4, 5, 6, 7))
    widths(ws, {"A": 6, "B": 24, "C": 52, "D": 24, "E": 38, "F": 24, "G": 42})
    ws.freeze_panes = "A2"

    # ---------- 全量明细 ----------
    ws = wb.create_sheet("全量明细")
    ws.append(["question_id", "题型", "问题", "标准答案", "模型答案",
               "是否正确", "归属模型", "检索命中"])
    style_header(ws, 1, 8)
    for i, r in enumerate(rows, start=2):
        ws.cell(i, 1, r["question_id"]).font = BODY_FONT
        ws.cell(i, 2, r["question_type"]).font = BODY_FONT
        ws.cell(i, 3, r["question"]).font = BODY_FONT
        ws.cell(i, 4, r["gold"]).font = BODY_FONT
        ws.cell(i, 5, r["pred"]).font = BODY_FONT
        c = ws.cell(i, 6, "是" if r.get("correct") else "否")
        c.font = GOOD_FONT if r.get("correct") else BAD_FONT
        ws.cell(i, 7, r.get("provider", "未知")).font = BODY_FONT
        ws.cell(i, 8, "是" if r.get("recall_hit") else "否").font = BODY_FONT
    style_body(ws, 2, n + 1, 1, 8, wrap_cols=(3, 4, 5))
    widths(ws, {"A": 12, "B": 24, "C": 52, "D": 24, "E": 36, "F": 9, "G": 24, "H": 9})
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = f"A1:H{n + 1}"

    for s in wb.worksheets:
        s.sheet_view.showGridLines = False

    wb.save(dst)
    print(f"已生成 {dst}：{n} 题，{ok} 对，错题 {len(wrong)}，"
          f"命中 {hc}，未命中 {mc}")


if __name__ == "__main__":
    main()
