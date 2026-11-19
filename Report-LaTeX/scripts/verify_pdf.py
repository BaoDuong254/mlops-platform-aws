#!/usr/bin/env python3
"""Check that the compiled PDF contains the Markdown's report text and tables."""
from __future__ import annotations

import hashlib
import json
import re
import subprocess
import unicodedata
from collections import Counter

from convert_report import DIAGRAM_CAPTIONS, FIGURE_CAPTIONS, FIGURE_ASSETS, HISTORICAL_NOTE, TABLE_CAPTIONS, MD, ROOT, convert


def normalized(text):
    # PDF line wrapping, automatic hyphenation, and typographic ligatures are
    # layout differences. All other characters, including digits, are retained.
    return re.sub(r"[\s\-\u00ad]+", "", unicodedata.normalize("NFKC", text))


def visible_text(token):
    parts = []
    for child in token.children:
        if child.type == "code_inline":
            bold_code = re.fullmatch(r"\*\*([A-Za-z0-9_-]+)\*\*", child.content)
            parts.append(bold_code[1] if bold_code else child.content)
        elif child.type == "text":
            parts.append(child.content)
        elif child.type in ("softbreak", "hardbreak"):
            parts.append(" ")
    return "".join(parts)


def run():
    files, _ = convert()
    for path, content in files.items():
        assert path.read_text() == content, f"Generated file differs: {path}"
    source = ROOT / "source/report.md"
    original = ROOT.parent / "report.md"
    if original.exists():
        assert original.read_bytes() == source.read_bytes(), "Root report.md has changed; review and refresh the source snapshot."
    manifest = json.loads((ROOT / "source/assets.json").read_text())
    for entry in manifest:
        assert hashlib.sha256((ROOT / entry["file"]).read_bytes()).hexdigest() == entry["sha256"]
        # In the repository also compare to the original screenshot. Standalone
        # ZIP builds retain the recorded checksum when originals are absent.
        original_image = ROOT.parent.parent / entry["source"]
        if original_image.is_file():
            assert hashlib.sha256(original_image.read_bytes()).hexdigest() == entry["sha256"]
    expected_grafana = {
        "Pictures/h06-hpa.png": "Screenshot From 2026-11-19 17-30-34.png",
        "Pictures/h07-failed-canary.png": "Screenshot From 2026-11-19 17-30-23.png",
        "Pictures/h08-successful-canary.png": "img_5.png",
    }
    for file, original_name in expected_grafana.items():
        entry = next(item for item in manifest if item["file"] == file)
        assert entry["source"].endswith("/images/" + original_name), f"Wrong Grafana mapping: {file}"
        assert entry.get("historical_replay") == "Grafana TestData CSV Content"

    chapters = "\n".join(path.read_text() for path in sorted((ROOT / "Sections").glob("chapter-*.tex")))
    # Count uses, rather than unique filenames, to catch duplicate screenshots.
    for file in ["architecture.png", "lifecycle.png", "network.png"] + [item[0] for group in FIGURE_ASSETS for item in group]:
        assert chapters.count("{Pictures/" + file + "}") == 1, f"Missing/duplicated image: {file}"
    expected_labels = {"architecture": "2.1", "lifecycle": "2.2", "network": "3.1"}
    expected_labels.update({f"h{n:02d}": f"4.{n}" for n in range(1, 10)})
    auxiliary = (ROOT / "build/main.aux").read_text()
    for label, number in expected_labels.items():
        assert auxiliary.count(r"\newlabel{fig:" + label + "}") == 1
        assert r"\newlabel{fig:" + label + "}{{" + number + "}" in auxiliary
    assert r"\FigureReference" not in chapters
    assert auxiliary.count(r"\contentsline {figure}") == 12
    main_source = (ROOT / "main.tex").read_text()
    assert r"\listoffigures" not in main_source and r"\listoftables" not in main_source
    assert "landscape" not in chapters and "Diagrams/" not in chapters
    for label, original_name in {"lifecycle": "img_4.png", "network": "img_3.png"}.items():
        entry = next(item for item in manifest if item["file"] == "Pictures/" + label + ".png")
        assert entry["source"].endswith("/images/" + original_name)
    grid_tables = re.findall(r"\\begin\{table\}.*?\\end\{table\}", chapters, re.S)
    assert len(grid_tables) == len(TABLE_CAPTIONS)
    assert all(r"\hline" in table and r"\begin{tabular}{|" in table for table in grid_tables)
    assert r"\renewcommand{\thesection}{\Roman{section}}" in (ROOT / "Sections/preamble.tex").read_text()
    font_manifest = json.loads((ROOT / "Fonts/manifest.json").read_text())
    for font in font_manifest["files"]:
        assert hashlib.sha256((ROOT / "Fonts" / font["file"]).read_bytes()).hexdigest() == font["sha256"]

    pdf = ROOT / "main.pdf"
    extracts = []
    pdf_pages = []
    for mode in ("-layout", "-raw"):
        output = subprocess.check_output(["pdftotext", mode, str(pdf), "-"], text=True)
        if mode == "-layout":
            pdf_pages = output.split("\f")
        lines = [line for line in output.splitlines() if not any(part in line for part in (
            "Trường Đại học Bách khoa", "Khoa Khoa học và Kỹ thuật Máy tính",
            "Báo cáo Điện toán đám mây", "Nền tảng MLOps trên AWS", "Năm học"))]
        extracts.append(normalized("\n".join(lines)))

    # A floating figure can be placed between the two page fragments of a
    # paragraph. Its caption is not part of that paragraph's source prose.
    prose_extracts = extracts.copy()
    for number, caption in enumerate(FIGURE_CAPTIONS, 1):
        label = normalized(f"Hình 4.{number}: {caption}")
        prose_extracts = [text.replace(label, "") for text in prose_extracts]
    for group in FIGURE_ASSETS:
        for index, (_, caption) in enumerate(group):
            if len(group) > 1:
                label = normalized(f"({chr(97 + index)}) {caption}")
                prose_extracts = [text.replace(label, "") for text in prose_extracts]
    prose_extracts = [text.replace(normalized(HISTORICAL_NOTE), "") for text in prose_extracts]
    for number, caption in {"2.1": DIAGRAM_CAPTIONS["architecture"],
                            "2.2": DIAGRAM_CAPTIONS["lifecycle"],
                            "3.1": DIAGRAM_CAPTIONS["network"]}.items():
        label = normalized(f"Hình {number}: {caption}")
        prose_extracts = [text.replace(label, "") for text in prose_extracts]

    counts = Counter()
    missing = []
    tokens = MD.parse(source.read_text())
    # Tables may float between page fragments of one paragraph. Remove their
    # independently checked cell content/captions from prose-only extracts.
    chapter_number = 0
    chapter_table = 0
    table_index = 0
    for index, token in enumerate(tokens):
        if token.type == "heading_open" and token.tag == "h1":
            chapter_number = int(re.search(r"\d+", tokens[index + 1].content)[0])
            chapter_table = 0
        if token.type == "table_open":
            end = index + 1
            while tokens[end].type != "table_close":
                end += 1
            cells = "".join(visible_text(t) for t in tokens[index:end] if t.type == "inline")
            chapter_table += 1
            caption = f"Bảng {chapter_number}.{chapter_table}: {TABLE_CAPTIONS[table_index]}"
            table_index += 1
            prose_extracts = [text.replace(normalized(cells), "").replace(normalized(caption), "") for text in prose_extracts]
    for index, token in enumerate(tokens):
        if token.type != "inline":
            continue
        previous = tokens[index - 1].type
        raw = token.content
        if previous == "paragraph_open":
            if raw.startswith(("![[", "**[", "_Hình ", "**Bảng 4.", "[  \nMAE_")) or raw.lstrip("_*").startswith("Hình thể hiện"):
                continue
            kind = "paragraphs"
        elif previous == "heading_open":
            kind = "headings"
        elif previous in ("th_open", "td_open"):
            kind = "table_cells"
        else:
            raise ValueError(previous)
        visible = visible_text(token)
        if kind == "headings":
            visible = re.sub(r"^(?:CHƯƠNG )?\d+(?:\.\d+)*\.\s*", "", visible)
        # Delta is typeset as math. The prose on both sides must still match.
        for segment in re.split(r"\(\\delta\s*=\s*0\{,\}01\)", visible):
            candidates = prose_extracts if kind == "paragraphs" else extracts
            if not any(normalized(segment) in extract for extract in candidates):
                missing.append({"kind": kind, "text": segment})
        counts[kind] += 1
    assert not missing, json.dumps(missing, ensure_ascii=False, indent=2)
    for caption in FIGURE_CAPTIONS:
        assert any(normalized(caption) in extract for extract in extracts), caption
    figure_pages = {}
    for label, number in expected_labels.items():
        matches = [index for index, page in enumerate(pdf_pages, 1)
                   if re.search(r"^\s*Hình\s+" + re.escape(number) + r":\s", page, re.M)]
        assert len(matches) == 1, f"Missing/duplicated PDF caption: {number}: {matches}"
        figure_pages[number] = matches[0]
    for number in ("4.6", "4.7", "4.8"):
        page = pdf_pages[figure_pages[number] - 1]
        assert normalized(HISTORICAL_NOTE) in normalized(page), f"Missing historical-data note: {number}"
    assert extracts[0].count(normalized(HISTORICAL_NOTE)) == 3
    full_text = "\n".join(pdf_pages)
    assert not re.search(r"xem\s+hình", full_text, re.I)
    reading_order = normalized(full_text)
    network_caption = normalized("Hình 3.1: " + DIAGRAM_CAPTIONS["network"])
    section_311 = re.search(r"(?m)^###\s+(3\.1\.1\.\s+.+)$", source.read_text())[1]
    section_312 = re.search(r"(?m)^###\s+(3\.1\.2\.\s+.+)$", source.read_text())[1]
    assert (reading_order.rfind(normalized(section_311)) < reading_order.find(network_caption)
            < reading_order.rfind(normalized(section_312))), "Figure 3.1 must stay inside section 3.1.1"
    log = ROOT / "build/main.log"
    if log.exists():
        problems = re.findall(r"^.*(?:Warning|Overfull|Missing character|ignored error|^!).*$", log.read_text(), re.M)
        assert not problems, "\n".join(problems)
    info = subprocess.check_output(["pdfinfo", str(pdf)], text=True)
    pages = int(re.search(r"^Pages:\s+(\d+)", info, re.M)[1])
    page_info = subprocess.check_output(["pdfinfo", "-f", "1", "-l", str(pages), str(pdf)], text=True)
    rotations = re.findall(r"^Page\s+\d+\s+rot:\s+(\d+)", page_info, re.M)
    assert len(rotations) == pages and set(rotations) == {"0"}, rotations
    blank_pages = []
    for number, page in enumerate(pdf_pages[:pages], 1):
        meaningful = [line for line in page.splitlines() if not any(part in line for part in (
            "Trường Đại học Bách khoa", "Khoa Khoa học và Kỹ thuật Máy tính",
            "Báo cáo Điện toán đám mây", "Năm học")) and not re.search(r"Page\s+\d+/\d+", line)]
        if len(normalized("".join(meaningful))) < 15:
            blank_pages.append(number)
    assert not blank_pages, f"Blank pages: {blank_pages}"
    result = dict(counts, evidence_captions=9, figure_count=12, figure_pdf_pages=figure_pages,
                  grafana_mapping="PASS", historical_notes=3, pages=pages,
                  roman_chapters=True, grid_tables=len(grid_tables), rotated_pages=0,
                  diagrams_from_images=True, figure_table_lists=False,
                  blank_pages=blank_pages, bundled_font="Computer Modern Unicode",
                  network_figure_in_section_311=True, figure_lead_in_text_removed=True,
                  source_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
                  pdf_sha256=hashlib.sha256(pdf.read_bytes()).hexdigest(), status="PASS")
    (ROOT / "source/pdf-verification.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    run()
