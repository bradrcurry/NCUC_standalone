# Extraction benchmark v0

| metric | hybrid | m1_cache_hybrid | pre_m1_db_hybrid | m1_db_hybrid | pre_m1_db_docling | m1_db_docling |
|---|---:|---:|---:|---:|---:|---:|
| expected | 798 | 798 | 798 | 798 | 798 | 798 |
| extracted | 629 | 629 | 628 | 628 | 619 | 619 |
| value_matched | 617 | 617 | 616 | 616 | 607 | 607 |
| value_accuracy (recall) | 0.7732 | 0.7732 | 0.7719 | 0.7719 | 0.7607 | 0.7607 |
| precision | 0.9809 | 0.9809 | 0.9809 | 0.9809 | 0.9806 | 0.9806 |
| unit_accuracy | 1.0 | 1.0 | 1.0 | 1.0 | 1.0 | 1.0 |
| rate_class_accuracy (summaries) | 1.0 | 1.0 | 1.0 | 1.0 | 1.0 | 1.0 |
| effective_date_accuracy | 0.65 | 0.65 | 0.65 | 0.65 | 0.65 | 0.65 |
| total_reconciliation | 0.7179 | 0.7179 | 0.7179 | 0.7179 | 0.7179 | 0.7179 |
| zero_doc_false_positives | 0 | 0 | 0 | 0 | 0 | 0 |
| evidence_snippet_rate | 1.0 | 1.0 | 1.0 | 1.0 | 1.0 | 1.0 |
| evidence_value_in_snippet_rate | 0.0334 | 0.0334 | 0.0334 | 0.0334 | 0.0339 | 0.0339 |
| provenance_page_rate | 0.9444 | 0.9444 | 0.8646 | 0.8694 | 0.021 | 0.021 |
| provenance_bbox_rate | 0.9444 | 0.9444 | 0.8646 | 0.8694 | 0.021 | 0.021 |
| provenance_table_cell_rate | 0.8394 | 0.8394 | 0.7627 | 0.7627 | 0.0162 | 0.0162 |
| provenance_value_quote_rate | 0.9444 | 0.9444 | 0.8646 | 0.8694 | 0.021 | 0.021 |
| matched_with_page_rate | 0.9449 | 0.9449 | 0.8636 | 0.8685 | 0.0214 | 0.0214 |
| seconds | 21.21 | 18.86 | 33.43 | 34.12 | 31.33 | 24.85 |

## Per document (value matched / expected, extracted, unit errs, class errs, status, text source, s)

| doc | hybrid | m1_cache_hybrid | pre_m1_db_hybrid | m1_db_hybrid | pre_m1_db_docling | m1_db_docling |
|---|---|---|---|---|---|---|
| dep_leaf600_2026-06-01 | 125/135, x125, u0, c0, parsed, pdfplumber, 1.101 | 125/135, x125, u0, c0, parsed, pdfplumber, 1.045 | 125/135, x125, u0, c0, parsed, pdfplumber, 3.231 | 125/135, x125, u0, c0, parsed, pdfplumber, 2.043 | 125/135, x125, u0, c0, parsed, pdfplumber, 2.4 | 125/135, x125, u0, c0, parsed, pdfplumber, 1.005 |
| dep_leaf600_2026-04-01 | 125/135, x125, u0, c0, parsed, pdfplumber, 1.001 | 125/135, x125, u0, c0, parsed, pdfplumber, 1.068 | 125/135, x125, u0, c0, parsed, pdfplumber, 3.442 | 125/135, x125, u0, c0, parsed, pdfplumber, 2.024 | 125/135, x125, u0, c0, parsed, pdfplumber, 1.878 | 125/135, x125, u0, c0, parsed, pdfplumber, 0.944 |
| dec_leaf99_2026-06-01 | 68/90, x68, u0, c0, parsed, pdfplumber, 0.524 | 68/90, x68, u0, c0, parsed, pdfplumber, 0.503 | 68/90, x68, u0, c0, parsed, pdfplumber, 1.467 | 68/90, x68, u0, c0, parsed, pdfplumber, 0.999 | 68/90, x68, u0, c0, parsed, pdfplumber, 0.929 | 68/90, x68, u0, c0, parsed, pdfplumber, 0.553 |
| dec_blk307_rev63 | 68/90, x68, u0, c0, parsed, pdfplumber, 0.398 | 68/90, x68, u0, c0, parsed, pdfplumber, 0.442 | 68/90, x68, u0, c0, parsed, pdfplumber, 0.812 | 68/90, x68, u0, c0, parsed, pdfplumber, 0.996 | 68/90, x68, u0, c0, parsed, pdfplumber, 0.822 | 68/90, x68, u0, c0, parsed, pdfplumber, 0.852 |
| dec_blk304_sub1285 | 54/72, x54, u0, c0, parsed, pdfplumber, 1.828 | 54/72, x54, u0, c0, parsed, pdfplumber, 1.662 | 54/72, x54, u0, c0, parsed, pdfplumber, 4.644 | 54/72, x54, u0, c0, parsed, pdfplumber, 2.657 | 54/72, x54, u0, c0, parsed, pdfplumber, 3.243 | 54/72, x54, u0, c0, parsed, pdfplumber, 3.054 |
| dep_blk13_sub1300 | 100/135, x112, u0, c0, parsed, pdfplumber, 1.363 | 100/135, x112, u0, c0, parsed, pdfplumber, 1.263 | 99/135, x111, u0, c0, parsed, pdfplumber, 3.457 | 99/135, x111, u0, c0, parsed, pdfplumber, 3.946 | 90/135, x102, u0, c0, parsed, pdfplumber, 2.108 | 90/135, x102, u0, c0, parsed, pdfplumber, 2.105 |
| dec_blk361_sub1146_fca | 56/72, x56, u0, c0, parsed, pdfplumber, 2.003 | 56/72, x56, u0, c0, parsed, pdfplumber, 1.679 | 56/72, x56, u0, c0, parsed, pdfplumber, 4.087 | 56/72, x56, u0, c0, parsed, pdfplumber, 4.036 | 56/72, x56, u0, c0, parsed, pdfplumber, 3.251 | 56/72, x56, u0, c0, parsed, pdfplumber, 3.254 |
| hd0029_dep_res | 5/6, x5, u0, c0, parsed, docling_artifact_sliced, 0.059 | 5/6, x5, u0, c0, parsed, docling_artifact_sliced, 0.035 | 5/6, x5, u0, c0, parsed, docling_artifact_sliced, 0.061 | 5/6, x5, u0, c0, parsed, docling_artifact_sliced, 0.076 | 5/6, x5, u0, c0, parsed, docling_artifact_sliced, 0.085 | 5/6, x5, u0, c0, parsed, docling_artifact_sliced, 0.083 |
| hd7378_dep_rtoud_2014 | 0/0, x0, u0, c0, empty, pdfplumber, 1.506 | 0/0, x0, u0, c0, empty, pdfplumber, 1.352 | 0/0, x0, u0, c0, empty, pdfplumber, 1.867 | 0/0, x0, u0, c0, empty, pdfplumber, 1.867 | 0/0, x0, u0, c0, empty, pdfplumber, 2.365 | 0/0, x0, u0, c0, empty, pdfplumber, 2.108 |
| hd3140_dep_rtoud44 | 6/10, x6, u0, c0, parsed, pdfplumber, 0.475 | 6/10, x6, u0, c0, parsed, pdfplumber, 0.465 | 6/10, x6, u0, c0, parsed, pdfplumber, 0.688 | 6/10, x6, u0, c0, parsed, pdfplumber, 1.045 | 6/10, x6, u0, c0, parsed, pdfplumber, 0.537 | 6/10, x6, u0, c0, parsed, pdfplumber, 0.739 |
| hd7380_dec_rs_scan | 0/29, x0, u0, c0, empty, docling_artifact_sliced, 0.051 | 0/29, x0, u0, c0, empty, docling_artifact_sliced, 0.05 | 0/29, x0, u0, c0, empty, docling_artifact_sliced, 0.088 | 0/29, x0, u0, c0, empty, docling_artifact_sliced, 0.079 | 0/29, x0, u0, c0, empty, docling_artifact_sliced, 0.065 | 0/29, x0, u0, c0, empty, docling_artifact_sliced, 0.088 |
| hd1875_dec_rs | 2/3, x2, u0, c0, parsed, docling_artifact_sliced, 0.029 | 2/3, x2, u0, c0, parsed, docling_artifact_sliced, 0.024 | 2/3, x2, u0, c0, parsed, docling_artifact_sliced, 0.053 | 2/3, x2, u0, c0, parsed, docling_artifact_sliced, 0.037 | 2/3, x2, u0, c0, parsed, docling_artifact_sliced, 0.043 | 2/3, x2, u0, c0, parsed, docling_artifact_sliced, 0.057 |
| hd0179_jaa | 0/0, x0, u0, c0, empty, docling_artifact_sliced, 0.233 | 0/0, x0, u0, c0, empty, docling_artifact_sliced, 0.194 | 0/0, x0, u0, c0, empty, docling_artifact_sliced, 0.32 | 0/0, x0, u0, c0, empty, docling_artifact_sliced, 0.286 | 0/0, x0, u0, c0, empty, docling_artifact_sliced, 0.258 | 0/0, x0, u0, c0, empty, docling_artifact_sliced, 0.407 |
| hd7365_jaa | 0/0, x0, u0, c0, empty, pdfplumber, 4.015 | 0/0, x0, u0, c0, empty, pdfplumber, 3.312 | 0/0, x0, u0, c0, empty, pdfplumber, 5.309 | 0/0, x0, u0, c0, empty, pdfplumber, 8.257 | 0/0, x0, u0, c0, empty, pdfplumber, 5.52 | 0/0, x0, u0, c0, empty, pdfplumber, 5.73 |
| hd5324_jaa | 0/0, x0, u0, c0, empty, pdfplumber, 5.614 | 0/0, x0, u0, c0, empty, pdfplumber, 5.052 | 0/0, x0, u0, c0, empty, pdfplumber, 3.152 | 0/0, x0, u0, c0, empty, pdfplumber, 4.095 | 0/0, x0, u0, c0, empty, pdfplumber, 6.269 | 0/0, x0, u0, c0, empty, pdfplumber, 3.049 |
| hd1882_jaa_rev3 | 8/8, x8, u0, c0, parsed, docling_artifact_sliced, 0.019 | 8/8, x8, u0, c0, parsed, docling_artifact_sliced, 0.013 | 8/8, x8, u0, c0, parsed, docling_artifact_sliced, 0.017 | 8/8, x8, u0, c0, parsed, docling_artifact_sliced, 0.042 | 8/8, x8, u0, c0, parsed, docling_artifact_sliced, 0.038 | 8/8, x8, u0, c0, parsed, docling_artifact_sliced, 0.021 |
| hd0378_cm | 0/0, x0, u0, c0, empty, pdfplumber, 0.626 | 0/0, x0, u0, c0, empty, pdfplumber, 0.469 | 0/0, x0, u0, c0, empty, pdfplumber, 0.488 | 0/0, x0, u0, c0, empty, pdfplumber, 1.301 | 0/0, x0, u0, c0, empty, pdfplumber, 0.781 | 0/0, x0, u0, c0, empty, pdfplumber, 0.442 |
| hd0379_r28 | 0/0, x0, u0, c0, empty, docling_artifact_sliced, 0.014 | 0/0, x0, u0, c0, empty, docling_artifact_sliced, 0.009 | 0/0, x0, u0, c0, empty, docling_artifact_sliced, 0.017 | 0/0, x0, u0, c0, empty, docling_artifact_sliced, 0.024 | 0/0, x0, u0, c0, empty, docling_artifact_sliced, 0.032 | 0/0, x0, u0, c0, empty, docling_artifact_sliced, 0.015 |
| hd7770_dec_riderlc | 0/12, x0, u0, c0, empty, docling_artifact_sliced, 0.145 | 0/12, x0, u0, c0, empty, docling_artifact_sliced, 0.103 | 0/12, x0, u0, c0, empty, docling_artifact_sliced, 0.096 | 0/12, x0, u0, c0, empty, docling_artifact_sliced, 0.14 | 0/12, x0, u0, c0, empty, docling_artifact_sliced, 0.448 | 0/12, x0, u0, c0, empty, docling_artifact_sliced, 0.212 |
| hd0396_ssr | 0/0, x0, u0, c0, empty, docling_artifact_sliced, 0.031 | 0/0, x0, u0, c0, empty, docling_artifact_sliced, 0.018 | 0/0, x0, u0, c0, empty, docling_artifact_sliced, 0.02 | 0/0, x0, u0, c0, empty, docling_artifact_sliced, 0.031 | 0/0, x0, u0, c0, empty, docling_artifact_sliced, 0.041 | 0/0, x0, u0, c0, empty, docling_artifact_sliced, 0.017 |
| hd7295_car | 0/0, x0, u0, c0, skipped_order, None, 0.031 | 0/0, x0, u0, c0, skipped_order, None, 0.024 | 0/0, x0, u0, c0, skipped_order, None, 0.023 | 0/0, x0, u0, c0, skipped_order, None, 0.028 | 0/0, x0, u0, c0, skipped_order, None, 0.043 | 0/0, x0, u0, c0, skipped_order, None, 0.022 |
| hd0392_pps | 0/1, x0, u0, c0, skipped_formula, None, 0.036 | 0/1, x0, u0, c0, skipped_formula, None, 0.019 | 0/1, x0, u0, c0, skipped_formula, None, 0.02 | 0/1, x0, u0, c0, skipped_formula, None, 0.026 | 0/1, x0, u0, c0, skipped_formula, None, 0.04 | 0/1, x0, u0, c0, skipped_formula, None, 0.024 |
| hd0403_cei | 0/0, x0, u0, c0, skipped_formula, None, 0.021 | 0/0, x0, u0, c0, skipped_formula, None, 0.008 | 0/0, x0, u0, c0, skipped_formula, None, 0.011 | 0/0, x0, u0, c0, skipped_formula, None, 0.014 | 0/0, x0, u0, c0, skipped_formula, None, 0.02 | 0/0, x0, u0, c0, skipped_formula, None, 0.012 |
| hd0408_rsnes | 0/0, x0, u0, c0, empty, docling_artifact_sliced, 0.025 | 0/0, x0, u0, c0, empty, docling_artifact_sliced, 0.012 | 0/0, x0, u0, c0, empty, docling_artifact_sliced, 0.015 | 0/0, x0, u0, c0, empty, docling_artifact_sliced, 0.018 | 0/0, x0, u0, c0, empty, docling_artifact_sliced, 0.028 | 0/0, x0, u0, c0, empty, docling_artifact_sliced, 0.014 |
| hd0182_jaa_order | 0/0, x0, u0, c0, skipped_reference, None, 0.015 | 0/0, x0, u0, c0, skipped_reference, None, 0.009 | 0/0, x0, u0, c0, skipped_reference, None, 0.01 | 0/0, x0, u0, c0, skipped_reference, None, 0.013 | 0/0, x0, u0, c0, skipped_reference, None, 0.016 | 0/0, x0, u0, c0, skipped_reference, None, 0.009 |
| hd7819_edit4_term | 0/0, x0, u0, c0, empty, pdfplumber, 0.044 | 0/0, x0, u0, c0, empty, pdfplumber, 0.029 | 0/0, x0, u0, c0, empty, pdfplumber, 0.035 | 0/0, x0, u0, c0, empty, pdfplumber, 0.044 | 0/0, x0, u0, c0, empty, pdfplumber, 0.067 | 0/0, x0, u0, c0, empty, pdfplumber, 0.032 |
