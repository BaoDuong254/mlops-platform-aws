#!/usr/bin/env python3
"""Convert the frozen Markdown source without rewriting its report prose.

Requires markdown-it-py only for conversion/checking, not for compiling LaTeX.
All editorial repairs are explicit below and documented in README.md.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
from collections import Counter
from pathlib import Path

from markdown_it import MarkdownIt
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
MD = MarkdownIt("commonmark").enable("table")
TABLE_CAPTIONS = [
    "Phân chia dữ liệu theo thời gian",
    "Các đặc trưng đầu vào của mô hình",
    "Công nghệ hạ tầng và điều phối tài nguyên",
    "Công nghệ huấn luyện, quản lý mô hình và serving",
    "Công nghệ CI/CD, triển khai và giám sát",
    "Các nhóm chức năng của nền tảng MLOps",
    "Các API chính của dịch vụ suy luận",
    "Các workflow GitHub Actions",
    "Các điều kiện chấp nhận Canary",
    "So sánh kết quả Validation Gate",
    "Kết quả các kịch bản kiểm thử tải",
    "Phân bổ lưu lượng trong quá trình phát hành thành công",
]
# Ratios are table column widths relative to the available paragraph width.
TABLE_WIDTHS = [
    [.34, .34, .32], [.38, .62], [.35, .65], [.35, .65], [.35, .65],
    [.23, .40, .37], [.31, .69], [.25, .75], [.42, .58],
    [.44, .28, .28], [.42, .27, .31], [.50, .25, .25],
]
# Keep existing references stable when the revised chapter removes a table.
TABLE_LABEL_IDS = [1, 2, 3, 4, 5, 6, 9, 10, 12, 14, 15, 16]
DIAGRAM_CAPTIONS = {
    "architecture": "Kiến trúc tổng thể nền tảng MLOps trên AWS.",
    "lifecycle": "Luồng dữ liệu và vòng đời mô hình MLOps.",
    "network": "Cấu trúc mạng và các thành phần hạ tầng triển khai trên AWS.",
}
FIGURE_CAPTIONS = [
    "Hạ tầng tính toán và các workloads đang hoạt động trên Amazon EKS.",
    "Kết quả kiểm duyệt mô hình trong hai trường hợp Approved và Rejected.",
    "Các phiên bản mô hình được quản lý trong MLflow Model Registry.",
    "Kết quả truy xuất thông tin mô hình và thực hiện dự đoán qua FastAPI.",
    "Trạng thái đồng bộ và các tài nguyên Kubernetes được quản lý bởi Argo CD.",
    "Diễn biến thông lượng yêu cầu, mức sử dụng CPU và số lượng replica trong thử nghiệm HPA.",
    "Biến động thông lượng, độ trễ P95 và phân phối lưu lượng ALB khi Canary bị từ chối.",
    "Thông lượng, độ trễ P95 và quá trình chuyển đổi lưu lượng khi Canary v3 được phát hành thành công.",
    "Kết quả thực thi tự động toàn bộ quy trình CI, Training và Release trên GitHub Actions.",
]
FIGURE_ASSETS = [
    [("h01-nodes.png", "Các worker nodes"), ("h01-workloads.png", "Các workloads trong namespace mlops")],
    [("h02-approved.png", "Candidate được phê duyệt"), ("h02-rejected.png", "Candidate bị từ chối")],
    [("h03-registry.png", "")],
    [("h04-model-info.png", "Thông tin mô hình đang phục vụ"), ("h04-predict.png", "Kết quả dự đoán")],
    [("h05-argocd.png", "")], [("h06-hpa.png", "")],
    [("h07-failed-canary.png", "Diễn biến Canary thất bại và khôi phục lưu lượng về Stable"),
     ("h07-git-revert.png", "Git revert commit do GitHub Actions tạo để hoàn tác Release")],
    [("h08-successful-canary.png", "")],
    [("h09-pipeline.png", "Các jobs của pipeline tự động"), ("h09-completed.png", "Kết quả Release và checkpoint completed")],
]
# Cropping is applied by graphicx at display time, never to the source pixels.
# Margins are (left, top, right, bottom), in original image pixels.
IMAGE_TRIMS = {
    "architecture.png": (140, 90, 185, 95),
    "h01-nodes.png": (365, 475, 30, 35),
    "h01-workloads.png": (715, 175, 40, 285),
    "h02-approved.png": (450, 300, 10, 0),
    "h02-rejected.png": (450, 300, 10, 0),
    "h03-registry.png": (250, 60, 440, 70),
    "h04-model-info.png": (112, 30, 95, 20),
    "h09-completed.png": (460, 55, 20, 0),
    # Hide browser chrome/full-screen hint, keeping all three dashboard panels.
    "h08-successful-canary.png": (0, 70, 0, 0),
    # Keep the revert and original release commits; omit the unrelated merge.
    "h07-git-revert.png": (0, 0, 0, 97),
}
HISTORICAL_NOTE = "Dữ liệu lịch sử được tái hiện trên Grafana bằng TestData CSV Content."
HISTORICAL_FIGURES = {6, 7, 8}


def graphic(name: str, maxheight: str, width: str = r"0.94\linewidth") -> str:
    options = ["width=" + width, "height=" + maxheight, "keepaspectratio"]
    if name in IMAGE_TRIMS:
        left, top, right, bottom = IMAGE_TRIMS[name]
        # The PNG's natural bounding box depends on its embedded DPI. Convert
        # pixel margins to bp so graphicx crops precisely and reproducibly.
        with Image.open(ROOT / "Pictures" / name) as img:
            dpi_x, dpi_y = img.info.get("dpi", (72, 72))
            assert left + right < img.width and top + bottom < img.height
        trim = (left * 72 / dpi_x, bottom * 72 / dpi_y,
                right * 72 / dpi_x, top * 72 / dpi_y)
        options += ["trim={" + " ".join(f"{value:.5f}bp" for value in trim) + "}", "clip"]
    return r"\includegraphics[" + ",".join(options) + "]{Pictures/" + name + "}"


def architecture() -> str:
    return "\n".join([
        r"\begin{figure}[!htbp]\centering",
        graphic("architecture.png", r"0.43\textheight", r"\linewidth"),
        r"\caption{Kiến trúc tổng thể nền tảng MLOps trên AWS.}",
        r"\label{fig:architecture}", r"\end{figure}",
    ])


def diagram_image(name: str) -> str:
    parts = [
        r"\begin{figure}[!htbp]\centering",
        graphic(name + ".png", r"0.62\textheight", r"0.86\linewidth"),
        r"\caption{" + DIAGRAM_CAPTIONS[name] + "}",
        r"\label{fig:" + name + "}", r"\end{figure}",
    ]
    if name == "network":
        # Figure 3.1 explains Terraform in 3.1.1; do not let it float past 3.1.2.
        parts.append(r"\FloatBarrier")
    return "\n".join(parts)


def escape(value: str) -> str:
    special = {"\\": r"\textbackslash{}", "&": r"\&", "%": r"\%", "$": r"\$",
               "#": r"\#", "_": r"\_", "{": r"\{", "}": r"\}",
               "~": r"\textasciitilde{}", "^": r"\textasciicircum{}"}
    return "".join(special.get(char, char) for char in value)


def inline(token) -> str:
    out = []
    for child in token.children or []:
        if child.type == "text":
            # The source contains un-delimited inline TeX. Supply math delimiters.
            parts = re.split(r"\(\\delta\s*=\s*0\{,\}01\)", child.content)
            out.append(r"\(\delta=0{,}01\)".join(escape(p) for p in parts))
        elif child.type == "code_inline":
            bold_code = re.fullmatch(r"\*\*([A-Za-z0-9_-]+)\*\*", child.content)
            out.append(r"\textbf{\code{" + bold_code[1] + "}}" if bold_code else r"\code{" + child.content + "}")
        elif child.type in ("softbreak", "hardbreak"):
            out.append(" ")
        elif child.type in ("strong_open", "em_open"):
            out.append(r"\textbf{" if child.type == "strong_open" else r"\emph{")
        elif child.type in ("strong_close", "em_close"):
            out.append("}")
        elif child.type == "link_open":
            out.append(r"\href{" + escape(child.attrGet("href")) + "}{")
        elif child.type == "link_close":
            out.append("}")
        else:
            raise ValueError(f"Unsupported inline token: {child.type}")
    return "".join(out)


def figure(number: int) -> str:
    label = "fig:h" + f"{number:02d}"
    parts = [r"\begin{figure}[!htbp]", r"\centering"]
    assets = FIGURE_ASSETS[number - 1]
    if len(assets) == 1:
        parts.append(graphic(assets[0][0], r"0.46\textheight"))
    else:
        for name, caption in assets:
            maxheight = r"0.27\textheight" if number == 4 else r"0.30\textheight"
            if number == 7:
                maxheight = r"0.40\textheight" if name == "h07-failed-canary.png" else r"0.12\textheight"
            parts += [r"\begin{subfigure}{\linewidth}", r"\centering",
                      graphic(name, maxheight),
                      r"\caption{" + escape(caption) + "}", r"\end{subfigure}\par\medskip"]
    parts += [r"\caption{" + escape(FIGURE_CAPTIONS[number - 1]) + "}",
              r"\label{" + label + "}"]
    if number in HISTORICAL_FIGURES:
        parts.append(r"\caption*{\footnotesize " + escape(HISTORICAL_NOTE) + "}")
    parts.append(r"\end{figure}")
    return "\n".join(parts)


def table(tokens, index: int) -> str:
    rows = []
    current = []
    for token in tokens:
        if token.type == "tr_open":
            current = []
        elif token.type == "inline":
            current.append(inline(token))
        elif token.type == "tr_close":
            rows.append(current)
    widths = TABLE_WIDTHS[index]
    assert all(len(row) == len(widths) for row in rows)
    columns = "|" + "|".join(r"L{\dimexpr " + str(w) + r"\ReportTableWidth-2\tabcolsep\relax}" for w in widths) + "|"
    header = " & ".join(r"\textbf{" + cell + "}" for cell in rows[0]) + r" \\"
    caption = TABLE_CAPTIONS[index]
    lines = [r"\begin{table}[!htbp]\centering",
             r"\fontsize{12}{15}\selectfont\setstretch{1.05}",
             r"\setlength{\tabcolsep}{4pt}\setlength{\arrayrulewidth}{0.4pt}\renewcommand{\arraystretch}{1.2}",
             r"\setlength{\ReportTableWidth}{\dimexpr\linewidth-" + str(len(widths) + 1) + r"\arrayrulewidth\relax}",
             r"\begin{tabular}{" + columns + "}",
             r"\hline", header, r"\hline"]
    lines += [" & ".join(row) + r" \\\hline" for row in rows[1:]]
    lines += [r"\end{tabular}",
              r"\caption{" + escape(caption) + r"}\label{tab:" + f"{TABLE_LABEL_IDS[index]:02d}" + "}", r"\end{table}"]
    return "\n".join(lines)


def convert():
    source = (ROOT / "source/report.md").read_text()
    tokens = MD.parse(source)
    chapters = {}
    chapter = 0
    table_count = 0
    figures = []
    pending_figure = False
    handled = Counter()
    i = 0
    while i < len(tokens):
        token = tokens[i]
        if token.type == "heading_open":
            title = tokens[i + 1].content
            level = int(token.tag[1:])
            target_chapter = int(re.match(r"(?:CHƯƠNG )?(\d+)", title)[1])
            if target_chapter != chapter:
                chapter = target_chapter
                chapters[chapter] = ["% Generated by scripts/convert_report.py from source/report.md.\n"]
                if level != 1:
                    chapters[chapter].append(r"\section{KẾT LUẬN VÀ HƯỚNG PHÁT TRIỂN}")
            clean = re.sub(r"^(?:CHƯƠNG )?\d+(?:\.\d+)*\.\s*", "", title)
            command = {1: "section", 2: "subsection", 3: "subsubsection"}[level]
            if level == 1:
                chapters[chapter].append(r"\FloatBarrier")
                chapters[chapter].append(r"\Needspace{6\baselineskip}")
                pending_figure = False
            elif chapter == 4 and (level == 2 or pending_figure):
                # Keep each experiment's screenshot before the next heading;
                # floats can still share a page with the following heading.
                chapters[chapter].append(r"\FloatBarrier")
                pending_figure = False
            chapters[chapter].append("\\" + command + "{" + escape(clean) + "}\n")
            handled["headings"] += 1
            i += 3
            continue
        out = chapters[chapter]
        if token.type == "paragraph_open":
            body = tokens[i + 1]
            raw = body.content
            if raw.startswith("![["):
                handled["resolved_obsidian_images"] += 1
            elif "PLACEHOLDER" in raw or re.match(r"\*\*\[Hình\s+\d+\.\d+", raw):
                match = re.search(r"Hình (\d\.\d)", raw)
                num = match[1]
                if num == "2.1":
                    out.append(architecture())
                else:
                    name = {"2.2": "lifecycle", "3.1": "network"}[num]
                    out.append(diagram_image(name))
                pending_figure = True
                handled["resolved_diagram_placeholders"] += 1
            elif raw.lstrip("_*").startswith("Hình thể hiện"):
                # Editorial drawing instructions are represented by the completed diagram.
                handled["resolved_editorial_figure_notes"] += 1
            elif re.match(r"\*\*\[H\d{2}", raw):
                number = int(re.search(r"H(\d{2})", raw)[1])
                out.append(figure(number))
                figures.append(number)
                pending_figure = True
            elif re.match(r"_Hình [234]\.\d+\.", raw):
                handled["captions_moved_to_figures"] += 1
            elif re.match(r"\*\*Bảng 4\.[12]\.", raw):
                handled["captions_moved_to_tables"] += 1
            elif raw.startswith("[  \nMAE_"):
                out.append(r"\begin{equation}" + "\n" + r"\operatorname{MAE}_{\text{candidate}} \leq \operatorname{MAE}_{\text{reference}}\times(1-\delta)" + "\n" + r"\label{eq:validation-gate}\end{equation}")
                handled["display_equations"] += 1
            else:
                out.append(inline(body) + "\n")
                handled["prose_paragraphs"] += 1
            i += 3
            continue
        if token.type == "table_open":
            end = i + 1
            while tokens[end].type != "table_close":
                end += 1
            out.append(table(tokens[i:end + 1], table_count))
            table_count += 1
            i = end + 1
            continue
        if token.type == "fence":
            assert token.info == "mermaid"
            # User-supplied images/img_4.png and img_3.png replace these blocks.
            # Never regenerate/re-render the diagrams from Mermaid.
            handled["mermaid_diagrams"] += 1
        elif token.type in ("bullet_list_open", "ordered_list_open"):
            out.append(r"\begin{" + ("itemize" if token.type == "bullet_list_open" else "enumerate") + "}")
            handled["lists"] += 1
        elif token.type in ("bullet_list_close", "ordered_list_close"):
            out.append(r"\end{" + ("itemize" if token.type == "bullet_list_close" else "enumerate") + "}")
        elif token.type == "list_item_open":
            out.append(r"\item ")
            handled["list_items"] += 1
        elif token.type == "list_item_close":
            pass
        elif token.type == "hr":
            out.append(r"\par\medskip\noindent\rule{\linewidth}{0.3pt}\par\medskip")
        else:
            raise ValueError(f"Unhandled block token: {token.type}")
        i += 1
    assert sorted(chapters) == [1, 2, 3, 4, 5]
    assert table_count == len(TABLE_CAPTIONS) == len(TABLE_WIDTHS) == len(TABLE_LABEL_IDS)
    assert figures == list(range(1, 10))
    files = {ROOT / f"Sections/chapter-{n:02d}.tex": "\n\n".join(lines) + "\n" for n, lines in chapters.items()}
    summary = dict(handled, chapters=5, tables=table_count, evidence_figures=len(figures), total_figures=12,
                   source_sha256=hashlib.sha256(source.encode()).hexdigest())
    files[ROOT / "source/conversion-summary.json"] = json.dumps(summary, ensure_ascii=False, indent=2) + "\n"
    return files, summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="Check generated files and image checksums without writing")
    args = parser.parse_args()
    files, summary = convert()
    for path, content in files.items():
        if args.check:
            assert path.read_text() == content, f"Generated file differs: {path}"
        else:
            path.write_text(content)
    for entry in json.loads((ROOT / "source/assets.json").read_text()):
        digest = hashlib.sha256((ROOT / entry["file"]).read_bytes()).hexdigest()
        assert digest == entry["sha256"], f"Asset changed: {entry['file']}"
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print("PASS: generated files and all copied assets match the frozen source." if args.check else "Generated all five chapters.")


if __name__ == "__main__":
    main()
