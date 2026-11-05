# latexmk configuration for the BME VIK thesis template
# Build: latexmk thesis.tex   |   Live rebuild: latexmk -pvc thesis.tex   |   Clean: latexmk -c

# The template requires XeLaTeX (see "% !TeX program = xelatex" in thesis.tex)
$pdf_mode = 5;

# -synctex=1: enables code <-> PDF jumping in the VS Code preview
# -interaction=nonstopmode: report errors instead of stopping to wait for input
# -file-line-error: print errors as file:line, so they are clickable
$xelatex = 'xelatex -synctex=1 -interaction=nonstopmode -file-line-error %O %S';

# Run bibtex when needed; also let 'latexmk -c' delete the generated .bbl
$bibtex_use = 2;

# Only build thesis.tex, even if latexmk is run without a file name
@default_files = ('thesis.tex');

# Extra files this template generates, removed by 'latexmk -c'
$clean_ext = 'thm lol synctex.gz xdv';
