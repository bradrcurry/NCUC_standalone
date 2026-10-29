cd /c/Python/Duke/Standalone
X="/c/Users/bradr/AppData/Local/Temp/claude/C--Python-Duke-Standalone/68255879-7747-4a8b-8255-1cff06db3f02/scratchpad/excerpts"; R="/c/Users/bradr/AppData/Local/Temp/claude/C--Python-Duke-Standalone/68255879-7747-4a8b-8255-1cff06db3f02/scratchpad/conv"
.venv312/Scripts/python.exe bench/phase2d/convert_corpus.py --excerpts $X --out $R/current_app_r2 --min-avail-gb 0.5 > $R/current_app_r2.log 2>&1; echo "current_app_r2 $?"
.venv312/Scripts/python.exe bench/phase2d/convert_corpus.py --excerpts $X --out $R/current_egret_m --variant egret_m --min-avail-gb 0.5 > $R/current_egret_m.log 2>&1; echo "current_egret_m $?"
export HF_HOME="$PWD/.cache-docling-next/hf"
.venv-docling-next/Scripts/python.exe bench/phase2d/convert_corpus.py --excerpts $X --out $R/next_app_r2 --min-avail-gb 0.5 > $R/next_app_r2.log 2>&1; echo "next_app_r2 $?"
.venv-docling-next/Scripts/python.exe bench/phase2d/convert_corpus.py --excerpts $X --out $R/next_nonthreaded --variant nonthreaded --min-avail-gb 0.5 > $R/next_nonthreaded.log 2>&1; echo "next_nonthreaded $?"
.venv-docling-next/Scripts/python.exe bench/phase2d/convert_corpus.py --excerpts $X --out $R/next_egret_m_nonthreaded --variant egret_m_nonthreaded --min-avail-gb 0.5 > $R/next_egret_m_nonthreaded.log 2>&1; echo "next_egret_m_nonthreaded $?"
