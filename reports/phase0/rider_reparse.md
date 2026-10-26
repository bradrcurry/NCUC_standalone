# Rider-summary re-parse: baseline `8c092b5` vs working tree

Documents: 37 (missing on disk: 0)

| metric | baseline / pdfplumber | baseline / pymupdf | repaired / pdfplumber | repaired / pymupdf |
|---|---:|---:|---:|---:|
| blocks | 320 | 248 | 348 | 348 |
| merged_blocks | 8 | 82 | 0 | 0 |
| ambiguous_blocks | 0 | 0 | 8 | 0 |
| footer_rows | 141 | 180 | 0 | 0 |
| implausible_cents_rows | 412 | 803 | 11 | 11 |
| jaa_cents_gt_1 | 96 | 96 | 32 | 31 |
| unit_ambiguous_rows | 0 | 0 | 0 | 18 |
| checksums | 393 | 293 | 423 | 409 |
| checksums_ok | 176 | 95 | 386 | 391 |

Checksum pass rate baseline/pdfplumber: 176/393 = 44.8%

Checksum pass rate baseline/pymupdf: 95/293 = 32.4%

Checksum pass rate repaired/pdfplumber: 386/423 = 91.3%

Checksum pass rate repaired/pymupdf: 391/409 = 95.6%

Parse time (s, all docs x 2 extractors): baseline 3.54, repaired 1.10
