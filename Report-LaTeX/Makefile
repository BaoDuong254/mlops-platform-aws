.PHONY: all clean regenerate verify
all:
	latexmk -xelatex main.tex
	cp build/main.pdf main.pdf
clean:
	latexmk -C
regenerate:
	python3 scripts/convert_report.py
verify:
	python3 scripts/convert_report.py --check
	python3 scripts/verify_pdf.py
