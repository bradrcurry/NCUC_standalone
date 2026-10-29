cd /c/Python/Duke/Standalone
X="/c/Users/bradr/AppData/Local/Temp/claude/C--Python-Duke-Standalone/68255879-7747-4a8b-8255-1cff06db3f02/scratchpad/excerpts"; R="/c/Users/bradr/AppData/Local/Temp/claude/C--Python-Duke-Standalone/68255879-7747-4a8b-8255-1cff06db3f02/scratchpad/mem"; O="/c/Users/bradr/AppData/Local/Temp/claude/C--Python-Duke-Standalone/68255879-7747-4a8b-8255-1cff06db3f02/scratchpad/ocr"; mkdir -p $R $O
PDF=data/historical/ncuc/e-2-sub-1207/32b7c724-edef-4a3a-bb45-85058c2ec503.pdf
NEXT_HF="$PWD/.cache-docling-next/hf"
# --- memory: bounded (app bounded.py, 8-page chunks) in both envs
nvidia-smi --query-gpu=timestamp,utilization.gpu,memory.used --format=csv,noheader,nounits -lms 500 > $R/nvsmi_bounded_current.csv & NV=$!
.venv312/Scripts/python.exe bench/bounded_memory.py --pdf $PDF --span 17 31 --min-free-gb 0.25 --scenarios native_span bounded_docling_span --out $R/bounded_current.json > $R/bounded_current.log 2>&1; echo "bounded_current $?"; kill $NV
nvidia-smi --query-gpu=timestamp,utilization.gpu,memory.used --format=csv,noheader,nounits -lms 500 > $R/nvsmi_bounded_next.csv & NV=$!
HF_HOME=$NEXT_HF .venv-docling-next/Scripts/python.exe bench/bounded_memory.py --pdf $PDF --span 17 31 --min-free-gb 0.25 --scenarios native_span bounded_docling_span --out $R/bounded_next.json > $R/bounded_next.log 2>&1; echo "bounded_next $?"; kill $NV
# --- memory: Docling page_range on the original PDF (no excerpt), and whole-document conversion
.venv312/Scripts/python.exe bench/phase2d/convert_corpus.py --excerpts $X --out $R/pagerange_current --docs hd5324_jaa --page-range-native --min-avail-gb 0.25 > $R/pagerange_current.log 2>&1; echo "pagerange_current $?"
HF_HOME=$NEXT_HF .venv-docling-next/Scripts/python.exe bench/phase2d/convert_corpus.py --excerpts $X --out $R/pagerange_next --docs hd5324_jaa --page-range-native --min-avail-gb 0.25 > $R/pagerange_next.log 2>&1; echo "pagerange_next $?"
HF_HOME=$NEXT_HF .venv-docling-next/Scripts/python.exe bench/phase2d/convert_corpus.py --excerpts $X --out $R/whole_next --docs hd5324_jaa --whole-source --min-avail-gb 0.25 > $R/whole_next.log 2>&1; echo "whole_next $?"
.venv312/Scripts/python.exe bench/phase2d/convert_corpus.py --excerpts $X --out $R/whole_current --docs hd5324_jaa --whole-source --min-avail-gb 0.25 > $R/whole_current.log 2>&1; echo "whole_current $?"
# --- OCR modes: one scanned doc + three digital docs, both envs
DOCS="hd7380_dec_rs_scan dep_leaf600_2026-06-01 dec_blk304_sub1285 hd0029_dep_res"
for mode in off auto tesseract full; do
  .venv312/Scripts/python.exe bench/phase2d/convert_corpus.py --excerpts $X --out $O/current_$mode --ocr $mode --docs $DOCS --min-avail-gb 0.25 > $O/current_$mode.log 2>&1; echo "ocr current $mode $?"
  HF_HOME=$NEXT_HF .venv-docling-next/Scripts/python.exe bench/phase2d/convert_corpus.py --excerpts $X --out $O/next_$mode --ocr $mode --docs $DOCS --min-avail-gb 0.25 > $O/next_$mode.log 2>&1; echo "ocr next $mode $?"
done
