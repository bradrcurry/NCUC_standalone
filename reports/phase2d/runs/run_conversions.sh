cd /c/Python/Duke/Standalone
X="/c/Users/bradr/AppData/Local/Temp/claude/C--Python-Duke-Standalone/68255879-7747-4a8b-8255-1cff06db3f02/scratchpad/excerpts"; R="/c/Users/bradr/AppData/Local/Temp/claude/C--Python-Duke-Standalone/68255879-7747-4a8b-8255-1cff06db3f02/scratchpad/conv"
mkdir -p $R
.venv312/Scripts/python.exe bench/phase2d/convert_corpus.py --excerpts $X --out $R/current_app --min-avail-gb 0.25 > $R/current_app.log 2>&1; echo "current_app exit $?"
export HF_HOME="$PWD/.cache-docling-next/hf"
.venv-docling-next/Scripts/python.exe bench/phase2d/convert_corpus.py --excerpts $X --out $R/next_app --min-avail-gb 0.25 > $R/next_app.log 2>&1; echo "next_app exit $?"
.venv-docling-next/Scripts/python.exe bench/phase2d/convert_corpus.py --excerpts $X --out $R/next_tfv2 --variant tfv2 --min-avail-gb 0.25 > $R/next_tfv2.log 2>&1; echo "next_tfv2 exit $?"
.venv-docling-next/Scripts/python.exe bench/phase2d/convert_corpus.py --excerpts $X --out $R/next_egret_m --variant egret_m --min-avail-gb 0.25 > $R/next_egret_m.log 2>&1; echo "next_egret_m exit $?"
