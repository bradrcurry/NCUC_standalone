# Python Environments

**Updated:** 2026-04-08

This repo should no longer be operated from the global Python `3.14` install.

The recommended setup is:

- `.venv312`
  - primary development environment
  - core repo work
  - tests
  - PDF/text extraction
  - Tesseract-based OCR helpers
- `.venv312-ocr`
  - OCR/document-intelligence environment
  - PaddleOCR / PP-StructureV3
  - Ollama GLM-OCR integration work
  - heavier OCR/runtime dependencies

## Why Two Environments

The project currently runs best on Python `3.12`, while some OCR/runtime
packages are heavier and more fragile than the core parsing stack.

Using two environments keeps:

- the main repo environment stable
- OCR experimentation isolated
- the project off the unsupported global Python `3.14` path

## Current State

Both environments have been created locally in the repo root:

- `.venv312`
- `.venv312-ocr`

Both use Python `3.12.10`.

The following were installed into both:

- editable project install
- `dev`
- `pdf`
- `ocr`

The OCR environment also has:

- `paddleocr`
- `paddlepaddle`
- `paddlex`

Current OCR runtime note:

- Paddle is currently installed and working in CPU mode on Windows
- `PPStructureV3` is importable
- `compiled_with_cuda = False` in the current Windows wheel

That means the OCR environment is usable now, but not GPU-accelerated yet.

## Activate The Main Environment

PowerShell:

```powershell
.\.venv312\Scripts\Activate.ps1
```

Verify:

```powershell
python -V
python -m pip show duke-rates
```

## Activate The OCR Environment

PowerShell:

```powershell
.\.venv312-ocr\Scripts\Activate.ps1
```

Recommended session variables:

```powershell
$env:PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK = "True"
$env:OLLAMA_HOST = "http://127.0.0.1:11434"
```

The Paddle variable avoids repeated model-hoster connectivity checks during
local development.

## Verification Commands

Main environment:

```powershell
.\.venv312\Scripts\python.exe -m pytest tests/test_document_intelligence.py tests/test_document_normalization.py -q
```

OCR environment:

```powershell
.\.venv312-ocr\Scripts\python.exe -c "import paddle; print(paddle.__version__, paddle.is_compiled_with_cuda())"
.\.venv312-ocr\Scripts\python.exe -c "import paddleocr; print(hasattr(paddleocr, 'PPStructureV3'))"
.\.venv312-ocr\Scripts\python.exe -c "from duke_rates.document_intelligence.normalization import PaddleStructureNormalizer, DocumentNormalizationConfig; print(PaddleStructureNormalizer(DocumentNormalizationConfig()).is_available())"
```

## Installation Notes

If you need to recreate the environments:

```powershell
py -3.12 -m venv .venv312
py -3.12 -m venv .venv312-ocr
.\.venv312\Scripts\python.exe -m pip install --upgrade pip
.\.venv312-ocr\Scripts\python.exe -m pip install --upgrade pip
.\.venv312\Scripts\python.exe -m pip install -e .[dev,pdf,ocr]
.\.venv312-ocr\Scripts\python.exe -m pip install -e .[dev,pdf,ocr]
.\.venv312-ocr\Scripts\python.exe -m pip install -e .[ocr-advanced]
.\.venv312-ocr\Scripts\python.exe -m pip install paddlepaddle
```

## GPU Note

This laptop has an RTX 4060 Laptop GPU, but the currently installed Windows
Paddle runtime is CPU-only.

So the practical recommendation is:

- use `.venv312` for normal repo work
- use `.venv312-ocr` for OCR/backend development right now
- treat Paddle GPU acceleration on Windows as a follow-up optimization, not a
  prerequisite

If GPU Paddle becomes important later, the cleanest path may be:

- a supported Windows GPU wheel if Paddle provides one for your exact stack, or
- a separate WSL/Linux OCR environment

## Operational Recommendation

Default to:

- `.venv312` for day-to-day parsing/analytics/doc work
- `.venv312-ocr` only when working on:
  - Paddle normalization
  - GLM-OCR fallback
  - OCR benchmarking
  - layout/table extraction experiments
