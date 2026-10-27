# Extraction benchmark v0

| metric | repaired | phase2a |
|---|---:|---:|
| expected | 798 | 798 |
| extracted | 619 | 619 |
| value_matched | 607 | 607 |
| value_accuracy (recall) | 0.7607 | 0.7607 |
| precision | 0.9806 | 0.9806 |
| unit_accuracy | 1.0 | 1.0 |
| rate_class_accuracy (summaries) | 1.0 | 1.0 |
| effective_date_accuracy | 0.65 | 0.65 |
| total_reconciliation | 0.7179 | 0.7179 |
| zero_doc_false_positives | 0 | 0 |
| evidence_snippet_rate | 1.0 | 1.0 |
| evidence_value_in_snippet_rate | 0.0339 | 0.0339 |
| seconds | 16.66 | 28.52 |

## Per document (value matched / expected, extracted, unit errs, class errs, status, text source, s)

| doc | repaired | phase2a |
|---|---|---|
| dep_leaf600_2026-06-01 | 125/135, x125, u0, c0, parsed, pdfplumber, 1.016 | 125/135, x125, u0, c0, parsed, pdfplumber, 1.995 |
| dep_leaf600_2026-04-01 | 125/135, x125, u0, c0, parsed, pdfplumber, 1.034 | 125/135, x125, u0, c0, parsed, pdfplumber, 1.558 |
| dec_leaf99_2026-06-01 | 68/90, x68, u0, c0, parsed, pdfplumber, 0.457 | 68/90, x68, u0, c0, parsed, pdfplumber, 0.803 |
| dec_blk307_rev63 | 68/90, x68, u0, c0, parsed, pdfplumber, 0.404 | 68/90, x68, u0, c0, parsed, pdfplumber, 0.752 |
| dec_blk304_sub1285 | 54/72, x54, u0, c0, parsed, pdfplumber, 1.646 | 54/72, x54, u0, c0, parsed, pdfplumber, 2.91 |
| dep_blk13_sub1300 | 90/135, x102, u0, c0, parsed, pdfplumber, 1.232 | 90/135, x102, u0, c0, parsed, pdfplumber, 1.957 |
| dec_blk361_sub1146_fca | 56/72, x56, u0, c0, parsed, pdfplumber, 2.091 | 56/72, x56, u0, c0, parsed, pdfplumber, 3.228 |
| hd0029_dep_res | 5/6, x5, u0, c0, parsed, docling_artifact_sliced, 0.041 | 5/6, x5, u0, c0, parsed, docling_artifact_sliced, 0.09 |
| hd7378_dep_rtoud_2014 | 0/0, x0, u0, c0, empty, pdfplumber, 1.215 | 0/0, x0, u0, c0, empty, pdfplumber, 2.284 |
| hd3140_dep_rtoud44 | 6/10, x6, u0, c0, parsed, pdfplumber, 0.34 | 6/10, x6, u0, c0, parsed, pdfplumber, 0.815 |
| hd7380_dec_rs_scan | 0/29, x0, u0, c0, empty, docling_artifact_sliced, 0.075 | 0/29, x0, u0, c0, empty, docling_artifact_sliced, 0.14 |
| hd1875_dec_rs | 2/3, x2, u0, c0, parsed, docling_artifact_sliced, 0.032 | 2/3, x2, u0, c0, parsed, docling_artifact_sliced, 0.076 |
| hd0179_jaa | 0/0, x0, u0, c0, empty, docling_artifact_sliced, 0.21 | 0/0, x0, u0, c0, empty, docling_artifact_sliced, 0.334 |
| hd7365_jaa | 0/0, x0, u0, c0, empty, pdfplumber, 3.271 | 0/0, x0, u0, c0, empty, pdfplumber, 5.107 |
| hd5324_jaa | 0/0, x0, u0, c0, empty, pdfplumber, 2.754 | 0/0, x0, u0, c0, empty, pdfplumber, 4.776 |
| hd1882_jaa_rev3 | 8/8, x8, u0, c0, parsed, docling_artifact_sliced, 0.012 | 8/8, x8, u0, c0, parsed, docling_artifact_sliced, 0.047 |
| hd0378_cm | 0/0, x0, u0, c0, empty, pdfplumber, 0.561 | 0/0, x0, u0, c0, empty, pdfplumber, 0.811 |
| hd0379_r28 | 0/0, x0, u0, c0, empty, docling_artifact_sliced, 0.009 | 0/0, x0, u0, c0, empty, docling_artifact_sliced, 0.054 |
| hd7770_dec_riderlc | 0/12, x0, u0, c0, empty, docling_artifact_sliced, 0.098 | 0/12, x0, u0, c0, empty, docling_artifact_sliced, 0.207 |
| hd0396_ssr | 0/0, x0, u0, c0, empty, docling_artifact_sliced, 0.027 | 0/0, x0, u0, c0, empty, docling_artifact_sliced, 0.071 |
| hd7295_car | 0/0, x0, u0, c0, skipped_order, None, 0.028 | 0/0, x0, u0, c0, skipped_order, None, 0.111 |
| hd0392_pps | 0/1, x0, u0, c0, skipped_formula, None, 0.028 | 0/1, x0, u0, c0, skipped_formula, None, 0.086 |
| hd0403_cei | 0/0, x0, u0, c0, skipped_formula, None, 0.017 | 0/0, x0, u0, c0, skipped_formula, None, 0.071 |
| hd0408_rsnes | 0/0, x0, u0, c0, empty, docling_artifact_sliced, 0.018 | 0/0, x0, u0, c0, empty, docling_artifact_sliced, 0.062 |
| hd0182_jaa_order | 0/0, x0, u0, c0, skipped_reference, None, 0.012 | 0/0, x0, u0, c0, skipped_reference, None, 0.059 |
| hd7819_edit4_term | 0/0, x0, u0, c0, empty, pdfplumber, 0.033 | 0/0, x0, u0, c0, empty, pdfplumber, 0.113 |
