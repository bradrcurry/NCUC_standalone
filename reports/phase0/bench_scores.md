# Extraction benchmark v0

| metric | baseline_8b77aa4 | repaired |
|---|---:|---:|
| expected | 798 | 798 |
| extracted | 630 | 619 |
| value_matched | 477 | 607 |
| value_accuracy (recall) | 0.5977 | 0.7607 |
| precision | 0.7571 | 0.9806 |
| unit_accuracy | 0.9748 | 1.0 |
| rate_class_accuracy (summaries) | 0.943 | 1.0 |
| effective_date_accuracy | 0.65 | 0.65 |
| total_reconciliation | 0.5256 | 0.7179 |
| zero_doc_false_positives | 0 | 0 |
| evidence_snippet_rate | 1.0 | 1.0 |
| evidence_value_in_snippet_rate | 0.0333 | 0.0339 |
| seconds | 16.8 | 16.66 |

## Per document (value matched / expected, extracted, unit errs, class errs, status, text source, s)

| doc | baseline_8b77aa4 | repaired |
|---|---|---|
| dep_leaf600_2026-06-01 | 125/135, x132, u5, c14, parsed, pdfplumber, 1.106 | 125/135, x125, u0, c0, parsed, pdfplumber, 1.016 |
| dep_leaf600_2026-04-01 | 125/135, x125, u0, c0, parsed, pdfplumber, 0.955 | 125/135, x125, u0, c0, parsed, pdfplumber, 1.034 |
| dec_leaf99_2026-06-01 | 48/90, x52, u0, c2, parsed, pdfplumber, 0.475 | 68/90, x68, u0, c0, parsed, pdfplumber, 0.457 |
| dec_blk307_rev63 | 48/90, x52, u0, c2, parsed, pdfplumber, 0.446 | 68/90, x68, u0, c0, parsed, pdfplumber, 0.404 |
| dec_blk304_sub1285 | 38/72, x44, u0, c2, parsed, pdfplumber, 1.961 | 54/72, x54, u0, c0, parsed, pdfplumber, 1.646 |
| dep_blk13_sub1300 | 38/135, x136, u7, c4, parsed, pdfplumber, 1.339 | 90/135, x102, u0, c0, parsed, pdfplumber, 1.232 |
| dec_blk361_sub1146_fca | 34/72, x68, u0, c2, parsed, pdfplumber, 1.851 | 56/72, x56, u0, c0, parsed, pdfplumber, 2.091 |
| hd0029_dep_res | 5/6, x5, u0, c0, parsed, docling_artifact_sliced, 0.054 | 5/6, x5, u0, c0, parsed, docling_artifact_sliced, 0.041 |
| hd7378_dep_rtoud_2014 | 0/0, x0, u0, c0, empty, pdfplumber, 1.364 | 0/0, x0, u0, c0, empty, pdfplumber, 1.215 |
| hd3140_dep_rtoud44 | 6/10, x6, u0, c0, parsed, pdfplumber, 0.576 | 6/10, x6, u0, c0, parsed, pdfplumber, 0.34 |
| hd7380_dec_rs_scan | 0/29, x0, u0, c0, empty, docling_artifact_sliced, 0.101 | 0/29, x0, u0, c0, empty, docling_artifact_sliced, 0.075 |
| hd1875_dec_rs | 2/3, x2, u0, c0, parsed, docling_artifact_sliced, 0.03 | 2/3, x2, u0, c0, parsed, docling_artifact_sliced, 0.032 |
| hd0179_jaa | 0/0, x0, u0, c0, empty, docling_artifact_sliced, 0.268 | 0/0, x0, u0, c0, empty, docling_artifact_sliced, 0.21 |
| hd7365_jaa | 0/0, x0, u0, c0, empty, pdfplumber, 2.817 | 0/0, x0, u0, c0, empty, pdfplumber, 3.271 |
| hd5324_jaa | 0/0, x0, u0, c0, empty, pdfplumber, 2.747 | 0/0, x0, u0, c0, empty, pdfplumber, 2.754 |
| hd1882_jaa_rev3 | 8/8, x8, u0, c0, parsed, docling_artifact_sliced, 0.009 | 8/8, x8, u0, c0, parsed, docling_artifact_sliced, 0.012 |
| hd0378_cm | 0/0, x0, u0, c0, empty, pdfplumber, 0.408 | 0/0, x0, u0, c0, empty, pdfplumber, 0.561 |
| hd0379_r28 | 0/0, x0, u0, c0, empty, docling_artifact_sliced, 0.013 | 0/0, x0, u0, c0, empty, docling_artifact_sliced, 0.009 |
| hd7770_dec_riderlc | 0/12, x0, u0, c0, empty, docling_artifact_sliced, 0.104 | 0/12, x0, u0, c0, empty, docling_artifact_sliced, 0.098 |
| hd0396_ssr | 0/0, x0, u0, c0, empty, docling_artifact_sliced, 0.025 | 0/0, x0, u0, c0, empty, docling_artifact_sliced, 0.027 |
| hd7295_car | 0/0, x0, u0, c0, skipped_order, None, 0.035 | 0/0, x0, u0, c0, skipped_order, None, 0.028 |
| hd0392_pps | 0/1, x0, u0, c0, skipped_formula, None, 0.019 | 0/1, x0, u0, c0, skipped_formula, None, 0.028 |
| hd0403_cei | 0/0, x0, u0, c0, skipped_formula, None, 0.034 | 0/0, x0, u0, c0, skipped_formula, None, 0.017 |
| hd0408_rsnes | 0/0, x0, u0, c0, empty, docling_artifact_sliced, 0.016 | 0/0, x0, u0, c0, empty, docling_artifact_sliced, 0.018 |
| hd0182_jaa_order | 0/0, x0, u0, c0, skipped_reference, None, 0.015 | 0/0, x0, u0, c0, skipped_reference, None, 0.012 |
| hd7819_edit4_term | 0/0, x0, u0, c0, empty, pdfplumber, 0.029 | 0/0, x0, u0, c0, empty, pdfplumber, 0.033 |
