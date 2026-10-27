# Extraction benchmark v0

| metric | legacy | docling | native | hybrid |
|---|---:|---:|---:|---:|
| expected | 798 | 798 | 798 | 798 |
| extracted | 619 | 599 | 553 | 629 |
| value_matched | 607 | 587 | 542 | 617 |
| value_accuracy (recall) | 0.7607 | 0.7356 | 0.6792 | 0.7732 |
| precision | 0.9806 | 0.98 | 0.9801 | 0.9809 |
| unit_accuracy | 1.0 | 1.0 | 1.0 | 1.0 |
| rate_class_accuracy (summaries) | 1.0 | 1.0 | 0.9791 | 1.0 |
| effective_date_accuracy | 0.65 | 0.65 | 0.65 | 0.65 |
| total_reconciliation | 0.7179 | 0.641 | 0.6154 | 0.7179 |
| zero_doc_false_positives | 0 | 0 | 0 | 0 |
| evidence_snippet_rate | 1.0 | 1.0 | 1.0 | 1.0 |
| evidence_value_in_snippet_rate | 0.0339 | 0.0351 | 0.0289 | 0.0334 |
| provenance_page_rate | 0.0 | 0.9866 | 0.9892 | 0.9444 |
| provenance_bbox_rate | 0.0 | 0.9866 | 0.9892 | 0.9444 |
| provenance_table_cell_rate | 0.0 | 0.8798 | 0.8734 | 0.8394 |
| provenance_value_quote_rate | 0.0 | 0.9866 | 0.9892 | 0.9444 |
| matched_with_page_rate | 0.0 | 0.9864 | 0.9889 | 0.9449 |
| seconds | 17.8 | 18.38 | 34.36 | 21.21 |

## Per document (value matched / expected, extracted, unit errs, class errs, status, text source, s)

| doc | legacy | docling | native | hybrid |
|---|---|---|---|---|
| dep_leaf600_2026-06-01 | 125/135, x125, u0, c0, parsed, pdfplumber, 1.135 | 125/135, x125, u0, c0, parsed, pdfplumber, 1.156 | 97/135, x97, u0, c0, parsed, pdfplumber, 2.298 | 125/135, x125, u0, c0, parsed, pdfplumber, 1.101 |
| dep_leaf600_2026-04-01 | 125/135, x125, u0, c0, parsed, pdfplumber, 0.976 | 122/135, x122, u0, c0, parsed, pdfplumber, 1.184 | 113/135, x113, u0, c11, parsed, pdfplumber, 2.361 | 125/135, x125, u0, c0, parsed, pdfplumber, 1.001 |
| dec_leaf99_2026-06-01 | 68/90, x68, u0, c0, parsed, pdfplumber, 0.572 | 68/90, x68, u0, c0, parsed, pdfplumber, 0.596 | 53/90, x53, u0, c0, parsed, pdfplumber, 1.049 | 68/90, x68, u0, c0, parsed, pdfplumber, 0.524 |
| dec_blk307_rev63 | 68/90, x68, u0, c0, parsed, pdfplumber, 0.413 | 68/90, x68, u0, c0, parsed, pdfplumber, 0.481 | 68/90, x68, u0, c0, parsed, pdfplumber, 0.957 | 68/90, x68, u0, c0, parsed, pdfplumber, 0.398 |
| dec_blk304_sub1285 | 54/72, x54, u0, c0, parsed, pdfplumber, 1.892 | 36/72, x36, u0, c0, parsed, pdfplumber, 1.924 | 54/72, x54, u0, c0, parsed, pdfplumber, 2.824 | 54/72, x54, u0, c0, parsed, pdfplumber, 1.828 |
| dep_blk13_sub1300 | 90/135, x102, u0, c0, parsed, pdfplumber, 1.457 | 99/135, x111, u0, c0, parsed, pdfplumber, 1.283 | 93/135, x104, u0, c0, parsed, pdfplumber, 2.833 | 100/135, x112, u0, c0, parsed, pdfplumber, 1.363 |
| dec_blk361_sub1146_fca | 56/72, x56, u0, c0, parsed, pdfplumber, 2.077 | 48/72, x48, u0, c0, parsed, pdfplumber, 2.06 | 48/72, x48, u0, c0, parsed, pdfplumber, 2.858 | 56/72, x56, u0, c0, parsed, pdfplumber, 2.003 |
| hd0029_dep_res | 5/6, x5, u0, c0, parsed, docling_artifact_sliced, 0.054 | 5/6, x5, u0, c0, parsed, docling_artifact_sliced, 0.074 | 5/6, x5, u0, c0, parsed, docling_artifact_sliced, 0.342 | 5/6, x5, u0, c0, parsed, docling_artifact_sliced, 0.059 |
| hd7378_dep_rtoud_2014 | 0/0, x0, u0, c0, empty, pdfplumber, 1.384 | 0/0, x0, u0, c0, empty, pdfplumber, 1.479 | 0/0, x0, u0, c0, empty, pdfplumber, 1.425 | 0/0, x0, u0, c0, empty, pdfplumber, 1.506 |
| hd3140_dep_rtoud44 | 6/10, x6, u0, c0, parsed, pdfplumber, 0.395 | 6/10, x6, u0, c0, parsed, pdfplumber, 0.426 | 6/10, x6, u0, c0, parsed, pdfplumber, 0.847 | 6/10, x6, u0, c0, parsed, pdfplumber, 0.475 |
| hd7380_dec_rs_scan | 0/29, x0, u0, c0, empty, docling_artifact_sliced, 0.091 | 0/29, x0, u0, c0, empty, docling_artifact_sliced, 0.066 | 0/29, x0, u0, c0, empty, docling_artifact_sliced, 0.068 | 0/29, x0, u0, c0, empty, docling_artifact_sliced, 0.051 |
| hd1875_dec_rs | 2/3, x2, u0, c0, parsed, docling_artifact_sliced, 0.029 | 2/3, x2, u0, c0, parsed, docling_artifact_sliced, 0.03 | 2/3, x2, u0, c0, parsed, docling_artifact_sliced, 0.291 | 2/3, x2, u0, c0, parsed, docling_artifact_sliced, 0.029 |
| hd0179_jaa | 0/0, x0, u0, c0, empty, docling_artifact_sliced, 0.217 | 0/0, x0, u0, c0, empty, docling_artifact_sliced, 0.259 | 0/0, x0, u0, c0, empty, docling_artifact_sliced, 3.538 | 0/0, x0, u0, c0, empty, docling_artifact_sliced, 0.233 |
| hd7365_jaa | 0/0, x0, u0, c0, empty, pdfplumber, 3.265 | 0/0, x0, u0, c0, empty, pdfplumber, 3.956 | 0/0, x0, u0, c0, empty, pdfplumber, 6.229 | 0/0, x0, u0, c0, empty, pdfplumber, 4.015 |
| hd5324_jaa | 0/0, x0, u0, c0, empty, pdfplumber, 3.052 | 0/0, x0, u0, c0, empty, pdfplumber, 2.715 | 0/0, x0, u0, c0, empty, pdfplumber, 4.897 | 0/0, x0, u0, c0, empty, pdfplumber, 5.614 |
| hd1882_jaa_rev3 | 8/8, x8, u0, c0, parsed, docling_artifact_sliced, 0.028 | 8/8, x8, u0, c0, parsed, docling_artifact_sliced, 0.015 | 3/8, x3, u0, c0, parsed, docling_artifact_sliced, 0.153 | 8/8, x8, u0, c0, parsed, docling_artifact_sliced, 0.019 |
| hd0378_cm | 0/0, x0, u0, c0, empty, pdfplumber, 0.465 | 0/0, x0, u0, c0, empty, pdfplumber, 0.405 | 0/0, x0, u0, c0, empty, pdfplumber, 0.985 | 0/0, x0, u0, c0, empty, pdfplumber, 0.626 |
| hd0379_r28 | 0/0, x0, u0, c0, empty, docling_artifact_sliced, 0.011 | 0/0, x0, u0, c0, empty, docling_artifact_sliced, 0.01 | 0/0, x0, u0, c0, empty, docling_artifact_sliced, 0.092 | 0/0, x0, u0, c0, empty, docling_artifact_sliced, 0.014 |
| hd7770_dec_riderlc | 0/12, x0, u0, c0, empty, docling_artifact_sliced, 0.099 | 0/12, x0, u0, c0, empty, docling_artifact_sliced, 0.097 | 0/12, x0, u0, c0, empty, docling_artifact_sliced, 0.138 | 0/12, x0, u0, c0, empty, docling_artifact_sliced, 0.145 |
| hd0396_ssr | 0/0, x0, u0, c0, empty, docling_artifact_sliced, 0.029 | 0/0, x0, u0, c0, empty, docling_artifact_sliced, 0.022 | 0/0, x0, u0, c0, empty, docling_artifact_sliced, 0.029 | 0/0, x0, u0, c0, empty, docling_artifact_sliced, 0.031 |
| hd7295_car | 0/0, x0, u0, c0, skipped_order, None, 0.05 | 0/0, x0, u0, c0, skipped_order, None, 0.026 | 0/0, x0, u0, c0, skipped_order, None, 0.031 | 0/0, x0, u0, c0, skipped_order, None, 0.031 |
| hd0392_pps | 0/1, x0, u0, c0, skipped_formula, None, 0.023 | 0/1, x0, u0, c0, skipped_formula, None, 0.023 | 0/1, x0, u0, c0, skipped_formula, None, 0.029 | 0/1, x0, u0, c0, skipped_formula, None, 0.036 |
| hd0403_cei | 0/0, x0, u0, c0, skipped_formula, None, 0.013 | 0/0, x0, u0, c0, skipped_formula, None, 0.015 | 0/0, x0, u0, c0, skipped_formula, None, 0.017 | 0/0, x0, u0, c0, skipped_formula, None, 0.021 |
| hd0408_rsnes | 0/0, x0, u0, c0, empty, docling_artifact_sliced, 0.018 | 0/0, x0, u0, c0, empty, docling_artifact_sliced, 0.019 | 0/0, x0, u0, c0, empty, docling_artifact_sliced, 0.02 | 0/0, x0, u0, c0, empty, docling_artifact_sliced, 0.025 |
| hd0182_jaa_order | 0/0, x0, u0, c0, skipped_reference, None, 0.008 | 0/0, x0, u0, c0, skipped_reference, None, 0.011 | 0/0, x0, u0, c0, skipped_reference, None, 0.014 | 0/0, x0, u0, c0, skipped_reference, None, 0.015 |
| hd7819_edit4_term | 0/0, x0, u0, c0, empty, pdfplumber, 0.05 | 0/0, x0, u0, c0, empty, pdfplumber, 0.049 | 0/0, x0, u0, c0, empty, pdfplumber, 0.036 | 0/0, x0, u0, c0, empty, pdfplumber, 0.044 |
