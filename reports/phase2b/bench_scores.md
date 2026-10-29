# Extraction benchmark v0

| metric | repaired | legacy | docling | native | hybrid |
|---|---:|---:|---:|---:|---:|
| expected | 798 | 798 | 798 | 798 | 798 |
| extracted | 619 | 619 | 599 | 553 | 630 |
| value_matched | 607 | 607 | 587 | 542 | 617 |
| value_accuracy (recall) | 0.7607 | 0.7607 | 0.7356 | 0.6792 | 0.7732 |
| precision | 0.9806 | 0.9806 | 0.98 | 0.9801 | 0.9794 |
| unit_accuracy | 1.0 | 1.0 | 1.0 | 1.0 | 1.0 |
| rate_class_accuracy (summaries) | 1.0 | 1.0 | 1.0 | 0.9791 | 1.0 |
| effective_date_accuracy | 0.65 | 0.65 | 0.65 | 0.65 | 0.65 |
| total_reconciliation | 0.7179 | 0.7179 | 0.641 | 0.6154 | 0.7179 |
| zero_doc_false_positives | 0 | 0 | 0 | 0 | 0 |
| evidence_snippet_rate | 1.0 | 1.0 | 1.0 | 1.0 | 1.0 |
| evidence_value_in_snippet_rate | 0.0339 | 0.0339 | 0.0351 | 0.0289 | 0.0333 |
| provenance_page_rate | 0.0 | 0.0 | 0.9866 | 0.9892 | 0.9444 |
| provenance_bbox_rate | 0.0 | 0.0 | 0.9866 | 0.9892 | 0.9444 |
| provenance_table_cell_rate | 0.0 | 0.0 | 0.8798 | 0.8734 | 0.8397 |
| provenance_value_quote_rate | 0.0 | 0.0 | 0.9866 | 0.9892 | 0.9444 |
| matched_with_page_rate | 0.0 | 0.0 | 0.9864 | 0.9889 | 0.9449 |
| seconds | 16.66 | 18.44 | 20.25 | 39.36 | 19.96 |

## Per document (value matched / expected, extracted, unit errs, class errs, status, text source, s)

| doc | repaired | legacy | docling | native | hybrid |
|---|---|---|---|---|---|
| dep_leaf600_2026-06-01 | 125/135, x125, u0, c0, parsed, pdfplumber, 1.016 | 125/135, x125, u0, c0, parsed, pdfplumber, 1.235 | 125/135, x125, u0, c0, parsed, pdfplumber, 1.153 | 97/135, x97, u0, c0, parsed, pdfplumber, 2.018 | 125/135, x125, u0, c0, parsed, pdfplumber, 1.112 |
| dep_leaf600_2026-04-01 | 125/135, x125, u0, c0, parsed, pdfplumber, 1.034 | 125/135, x125, u0, c0, parsed, pdfplumber, 1.214 | 122/135, x122, u0, c0, parsed, pdfplumber, 1.307 | 113/135, x113, u0, c11, parsed, pdfplumber, 2.475 | 125/135, x125, u0, c0, parsed, pdfplumber, 1.097 |
| dec_leaf99_2026-06-01 | 68/90, x68, u0, c0, parsed, pdfplumber, 0.457 | 68/90, x68, u0, c0, parsed, pdfplumber, 0.505 | 68/90, x68, u0, c0, parsed, pdfplumber, 0.763 | 53/90, x53, u0, c0, parsed, pdfplumber, 1.245 | 68/90, x68, u0, c0, parsed, pdfplumber, 0.553 |
| dec_blk307_rev63 | 68/90, x68, u0, c0, parsed, pdfplumber, 0.404 | 68/90, x68, u0, c0, parsed, pdfplumber, 0.52 | 68/90, x68, u0, c0, parsed, pdfplumber, 0.833 | 68/90, x68, u0, c0, parsed, pdfplumber, 1.209 | 68/90, x68, u0, c0, parsed, pdfplumber, 0.498 |
| dec_blk304_sub1285 | 54/72, x54, u0, c0, parsed, pdfplumber, 1.646 | 54/72, x54, u0, c0, parsed, pdfplumber, 1.771 | 36/72, x36, u0, c0, parsed, pdfplumber, 3.435 | 54/72, x54, u0, c0, parsed, pdfplumber, 3.136 | 54/72, x54, u0, c0, parsed, pdfplumber, 2.444 |
| dep_blk13_sub1300 | 90/135, x102, u0, c0, parsed, pdfplumber, 1.232 | 90/135, x102, u0, c0, parsed, pdfplumber, 1.181 | 99/135, x111, u0, c0, parsed, pdfplumber, 1.589 | 93/135, x104, u0, c0, parsed, pdfplumber, 2.967 | 100/135, x113, u0, c0, parsed, pdfplumber, 1.534 |
| dec_blk361_sub1146_fca | 56/72, x56, u0, c0, parsed, pdfplumber, 2.091 | 56/72, x56, u0, c0, parsed, pdfplumber, 2.137 | 48/72, x48, u0, c0, parsed, pdfplumber, 2.146 | 48/72, x48, u0, c0, parsed, pdfplumber, 3.367 | 56/72, x56, u0, c0, parsed, pdfplumber, 1.994 |
| hd0029_dep_res | 5/6, x5, u0, c0, parsed, docling_artifact_sliced, 0.041 | 5/6, x5, u0, c0, parsed, docling_artifact_sliced, 0.046 | 5/6, x5, u0, c0, parsed, docling_artifact_sliced, 0.057 | 5/6, x5, u0, c0, parsed, docling_artifact_sliced, 0.395 | 5/6, x5, u0, c0, parsed, docling_artifact_sliced, 0.045 |
| hd7378_dep_rtoud_2014 | 0/0, x0, u0, c0, empty, pdfplumber, 1.215 | 0/0, x0, u0, c0, empty, pdfplumber, 1.341 | 0/0, x0, u0, c0, empty, pdfplumber, 1.475 | 0/0, x0, u0, c0, empty, pdfplumber, 2.22 | 0/0, x0, u0, c0, empty, pdfplumber, 1.166 |
| hd3140_dep_rtoud44 | 6/10, x6, u0, c0, parsed, pdfplumber, 0.34 | 6/10, x6, u0, c0, parsed, pdfplumber, 0.366 | 6/10, x6, u0, c0, parsed, pdfplumber, 0.444 | 6/10, x6, u0, c0, parsed, pdfplumber, 1.147 | 6/10, x6, u0, c0, parsed, pdfplumber, 0.495 |
| hd7380_dec_rs_scan | 0/29, x0, u0, c0, empty, docling_artifact_sliced, 0.075 | 0/29, x0, u0, c0, empty, docling_artifact_sliced, 0.083 | 0/29, x0, u0, c0, empty, docling_artifact_sliced, 0.062 | 0/29, x0, u0, c0, empty, docling_artifact_sliced, 0.081 | 0/29, x0, u0, c0, empty, docling_artifact_sliced, 0.071 |
| hd1875_dec_rs | 2/3, x2, u0, c0, parsed, docling_artifact_sliced, 0.032 | 2/3, x2, u0, c0, parsed, docling_artifact_sliced, 0.028 | 2/3, x2, u0, c0, parsed, docling_artifact_sliced, 0.043 | 2/3, x2, u0, c0, parsed, docling_artifact_sliced, 0.38 | 2/3, x2, u0, c0, parsed, docling_artifact_sliced, 0.039 |
| hd0179_jaa | 0/0, x0, u0, c0, empty, docling_artifact_sliced, 0.21 | 0/0, x0, u0, c0, empty, docling_artifact_sliced, 0.243 | 0/0, x0, u0, c0, empty, docling_artifact_sliced, 0.211 | 0/0, x0, u0, c0, empty, docling_artifact_sliced, 4.247 | 0/0, x0, u0, c0, empty, docling_artifact_sliced, 0.236 |
| hd7365_jaa | 0/0, x0, u0, c0, empty, pdfplumber, 3.271 | 0/0, x0, u0, c0, empty, pdfplumber, 3.573 | 0/0, x0, u0, c0, empty, pdfplumber, 3.311 | 0/0, x0, u0, c0, empty, pdfplumber, 6.931 | 0/0, x0, u0, c0, empty, pdfplumber, 2.992 |
| hd5324_jaa | 0/0, x0, u0, c0, empty, pdfplumber, 2.754 | 0/0, x0, u0, c0, empty, pdfplumber, 3.165 | 0/0, x0, u0, c0, empty, pdfplumber, 2.608 | 0/0, x0, u0, c0, empty, pdfplumber, 6.141 | 0/0, x0, u0, c0, empty, pdfplumber, 4.732 |
| hd1882_jaa_rev3 | 8/8, x8, u0, c0, parsed, docling_artifact_sliced, 0.012 | 8/8, x8, u0, c0, parsed, docling_artifact_sliced, 0.023 | 8/8, x8, u0, c0, parsed, docling_artifact_sliced, 0.02 | 3/8, x3, u0, c0, parsed, docling_artifact_sliced, 0.18 | 8/8, x8, u0, c0, parsed, docling_artifact_sliced, 0.028 |
| hd0378_cm | 0/0, x0, u0, c0, empty, pdfplumber, 0.561 | 0/0, x0, u0, c0, empty, pdfplumber, 0.582 | 0/0, x0, u0, c0, empty, pdfplumber, 0.45 | 0/0, x0, u0, c0, empty, pdfplumber, 0.821 | 0/0, x0, u0, c0, empty, pdfplumber, 0.543 |
| hd0379_r28 | 0/0, x0, u0, c0, empty, docling_artifact_sliced, 0.009 | 0/0, x0, u0, c0, empty, docling_artifact_sliced, 0.019 | 0/0, x0, u0, c0, empty, docling_artifact_sliced, 0.019 | 0/0, x0, u0, c0, empty, docling_artifact_sliced, 0.086 | 0/0, x0, u0, c0, empty, docling_artifact_sliced, 0.02 |
| hd7770_dec_riderlc | 0/12, x0, u0, c0, empty, docling_artifact_sliced, 0.098 | 0/12, x0, u0, c0, empty, docling_artifact_sliced, 0.162 | 0/12, x0, u0, c0, empty, docling_artifact_sliced, 0.133 | 0/12, x0, u0, c0, empty, docling_artifact_sliced, 0.121 | 0/12, x0, u0, c0, empty, docling_artifact_sliced, 0.127 |
| hd0396_ssr | 0/0, x0, u0, c0, empty, docling_artifact_sliced, 0.027 | 0/0, x0, u0, c0, empty, docling_artifact_sliced, 0.039 | 0/0, x0, u0, c0, empty, docling_artifact_sliced, 0.023 | 0/0, x0, u0, c0, empty, docling_artifact_sliced, 0.03 | 0/0, x0, u0, c0, empty, docling_artifact_sliced, 0.04 |
| hd7295_car | 0/0, x0, u0, c0, skipped_order, None, 0.028 | 0/0, x0, u0, c0, skipped_order, None, 0.051 | 0/0, x0, u0, c0, skipped_order, None, 0.039 | 0/0, x0, u0, c0, skipped_order, None, 0.022 | 0/0, x0, u0, c0, skipped_order, None, 0.047 |
| hd0392_pps | 0/1, x0, u0, c0, skipped_formula, None, 0.028 | 0/1, x0, u0, c0, skipped_formula, None, 0.034 | 0/1, x0, u0, c0, skipped_formula, None, 0.024 | 0/1, x0, u0, c0, skipped_formula, None, 0.037 | 0/1, x0, u0, c0, skipped_formula, None, 0.033 |
| hd0403_cei | 0/0, x0, u0, c0, skipped_formula, None, 0.017 | 0/0, x0, u0, c0, skipped_formula, None, 0.016 | 0/0, x0, u0, c0, skipped_formula, None, 0.023 | 0/0, x0, u0, c0, skipped_formula, None, 0.017 | 0/0, x0, u0, c0, skipped_formula, None, 0.021 |
| hd0408_rsnes | 0/0, x0, u0, c0, empty, docling_artifact_sliced, 0.018 | 0/0, x0, u0, c0, empty, docling_artifact_sliced, 0.029 | 0/0, x0, u0, c0, empty, docling_artifact_sliced, 0.021 | 0/0, x0, u0, c0, empty, docling_artifact_sliced, 0.032 | 0/0, x0, u0, c0, empty, docling_artifact_sliced, 0.031 |
| hd0182_jaa_order | 0/0, x0, u0, c0, skipped_reference, None, 0.012 | 0/0, x0, u0, c0, skipped_reference, None, 0.016 | 0/0, x0, u0, c0, skipped_reference, None, 0.013 | 0/0, x0, u0, c0, skipped_reference, None, 0.014 | 0/0, x0, u0, c0, skipped_reference, None, 0.017 |
| hd7819_edit4_term | 0/0, x0, u0, c0, empty, pdfplumber, 0.033 | 0/0, x0, u0, c0, empty, pdfplumber, 0.057 | 0/0, x0, u0, c0, empty, pdfplumber, 0.044 | 0/0, x0, u0, c0, empty, pdfplumber, 0.039 | 0/0, x0, u0, c0, empty, pdfplumber, 0.05 |
