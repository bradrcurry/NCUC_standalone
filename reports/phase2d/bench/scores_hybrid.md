# Extraction benchmark v0

| metric | hybrid__committed | hybrid__current_app | hybrid__next_app | nextenv_hybrid__next_app | hybrid__next_tfv2 | hybrid__next_egret_m |
|---|---:|---:|---:|---:|---:|---:|
| expected | 798 | 798 | 798 | 798 | 798 | 798 |
| extracted | 629 | 629 | 629 | 629 | 636 | 629 |
| value_matched | 617 | 617 | 617 | 617 | 610 | 617 |
| value_accuracy (recall) | 0.7732 | 0.7732 | 0.7732 | 0.7732 | 0.7644 | 0.7732 |
| precision | 0.9809 | 0.9809 | 0.9809 | 0.9809 | 0.9591 | 0.9809 |
| unit_accuracy | 1.0 | 1.0 | 1.0 | 1.0 | 1.0 | 1.0 |
| rate_class_accuracy (summaries) | 1.0 | 1.0 | 1.0 | 1.0 | 0.9966 | 1.0 |
| effective_date_accuracy | 0.65 | 0.65 | 0.65 | 0.65 | 0.65 | 0.65 |
| total_reconciliation | 0.7179 | 0.7179 | 0.7179 | 0.7179 | 0.7179 | 0.7179 |
| zero_doc_false_positives | 0 | 0 | 0 | 0 | 0 | 0 |
| evidence_snippet_rate | 1.0 | 1.0 | 1.0 | 1.0 | 1.0 | 1.0 |
| evidence_value_in_snippet_rate | 0.0334 | 0.0334 | 0.0334 | 0.0334 | 0.0236 | 0.0334 |
| provenance_page_rate | 0.9444 | 0.9523 | 0.9189 | 0.9189 | 0.3852 | 0.9841 |
| provenance_bbox_rate | 0.9444 | 0.9523 | 0.9189 | 0.9189 | 0.3852 | 0.9841 |
| provenance_table_cell_rate | 0.8394 | 0.8347 | 0.8045 | 0.8045 | 0.3208 | 0.868 |
| provenance_value_quote_rate | 0.9444 | 0.9523 | 0.9189 | 0.9189 | 0.3852 | 0.9841 |
| matched_with_page_rate | 0.9449 | 0.953 | 0.919 | 0.919 | 0.3623 | 0.9854 |
| seconds | 24.14 | 22.48 | 20.24 | 23.59 | 17.98 | 20.18 |

## Per document (value matched / expected, extracted, unit errs, class errs, status, text source, s)

| doc | hybrid__committed | hybrid__current_app | hybrid__next_app | nextenv_hybrid__next_app | hybrid__next_tfv2 | hybrid__next_egret_m |
|---|---|---|---|---|---|---|
| dep_leaf600_2026-06-01 | 125/135, x125, u0, c0, parsed, pdfplumber, 1.204 | 125/135, x125, u0, c0, parsed, pdfplumber, 1.356 | 125/135, x125, u0, c0, parsed, pdfplumber, 1.247 | 125/135, x125, u0, c0, parsed, pdfplumber, 4.265 | 125/135, x125, u0, c0, parsed, pdfplumber, 1.334 | 125/135, x125, u0, c0, parsed, pdfplumber, 1.924 |
| dep_leaf600_2026-04-01 | 125/135, x125, u0, c0, parsed, pdfplumber, 1.938 | 125/135, x125, u0, c0, parsed, pdfplumber, 1.469 | 125/135, x125, u0, c0, parsed, pdfplumber, 1.329 | 125/135, x125, u0, c0, parsed, pdfplumber, 1.273 | 129/135, x137, u0, c1, parsed, pdfplumber, 1.319 | 125/135, x125, u0, c0, parsed, pdfplumber, 1.268 |
| dec_leaf99_2026-06-01 | 68/90, x68, u0, c0, parsed, pdfplumber, 0.693 | 68/90, x68, u0, c0, parsed, pdfplumber, 0.69 | 68/90, x68, u0, c0, parsed, pdfplumber, 0.648 | 68/90, x68, u0, c0, parsed, pdfplumber, 0.488 | 68/90, x68, u0, c0, parsed, pdfplumber, 0.589 | 68/90, x68, u0, c0, parsed, pdfplumber, 0.587 |
| dec_blk307_rev63 | 68/90, x68, u0, c0, parsed, pdfplumber, 0.761 | 68/90, x68, u0, c0, parsed, pdfplumber, 0.57 | 68/90, x68, u0, c0, parsed, pdfplumber, 0.643 | 68/90, x68, u0, c0, parsed, pdfplumber, 0.389 | 68/90, x68, u0, c0, parsed, pdfplumber, 0.56 | 68/90, x68, u0, c0, parsed, pdfplumber, 0.549 |
| dec_blk304_sub1285 | 54/72, x54, u0, c0, parsed, pdfplumber, 2.613 | 54/72, x54, u0, c0, parsed, pdfplumber, 2.262 | 54/72, x54, u0, c0, parsed, pdfplumber, 2.163 | 54/72, x54, u0, c0, parsed, pdfplumber, 2.012 | 54/72, x54, u0, c0, parsed, pdfplumber, 1.842 | 54/72, x54, u0, c0, parsed, pdfplumber, 2.122 |
| dep_blk13_sub1300 | 100/135, x112, u0, c0, parsed, pdfplumber, 1.687 | 100/135, x112, u0, c0, parsed, pdfplumber, 1.659 | 100/135, x112, u0, c0, parsed, pdfplumber, 1.464 | 100/135, x112, u0, c0, parsed, pdfplumber, 1.528 | 95/135, x113, u0, c1, parsed, pdfplumber, 1.494 | 100/135, x112, u0, c0, parsed, pdfplumber, 1.454 |
| dec_blk361_sub1146_fca | 56/72, x56, u0, c0, parsed, pdfplumber, 2.146 | 56/72, x56, u0, c0, parsed, pdfplumber, 2.43 | 56/72, x56, u0, c0, parsed, pdfplumber, 2.78 | 56/72, x56, u0, c0, parsed, pdfplumber, 2.318 | 56/72, x56, u0, c0, parsed, pdfplumber, 2.047 | 56/72, x56, u0, c0, parsed, pdfplumber, 2.135 |
| hd0029_dep_res | 5/6, x5, u0, c0, parsed, docling_artifact_sliced, 0.047 | 5/6, x5, u0, c0, parsed, docling_artifact_sliced, 0.066 | 5/6, x5, u0, c0, parsed, docling_artifact_sliced, 0.058 | 5/6, x5, u0, c0, parsed, docling_artifact_sliced, 0.047 | 5/6, x5, u0, c0, parsed, docling_artifact_sliced, 0.049 | 5/6, x5, u0, c0, parsed, docling_artifact_sliced, 0.05 |
| hd7378_dep_rtoud_2014 | 0/0, x0, u0, c0, empty, pdfplumber, 1.462 | 0/0, x0, u0, c0, empty, pdfplumber, 1.742 | 0/0, x0, u0, c0, empty, pdfplumber, 1.785 | 0/0, x0, u0, c0, empty, pdfplumber, 1.446 | 0/0, x0, u0, c0, empty, pdfplumber, 1.321 | 0/0, x0, u0, c0, empty, pdfplumber, 1.473 |
| hd3140_dep_rtoud44 | 6/10, x6, u0, c0, parsed, pdfplumber, 0.594 | 6/10, x6, u0, c0, parsed, pdfplumber, 0.62 | 6/10, x6, u0, c0, parsed, pdfplumber, 0.479 | 6/10, x6, u0, c0, parsed, pdfplumber, 0.49 | 6/10, x6, u0, c0, parsed, pdfplumber, 0.445 | 6/10, x6, u0, c0, parsed, pdfplumber, 0.431 |
| hd7380_dec_rs_scan | 0/29, x0, u0, c0, empty, docling_artifact_sliced, 0.088 | 0/29, x0, u0, c0, empty, docling_artifact_sliced, 0.152 | 0/29, x0, u0, c0, empty, docling_artifact_sliced, 0.085 | 0/29, x0, u0, c0, empty, docling_artifact_sliced, 0.079 | 0/29, x0, u0, c0, empty, docling_artifact_sliced, 0.069 | 0/29, x0, u0, c0, empty, docling_artifact_sliced, 0.068 |
| hd1875_dec_rs | 2/3, x2, u0, c0, parsed, docling_artifact_sliced, 0.046 | 2/3, x2, u0, c0, parsed, docling_artifact_sliced, 0.045 | 2/3, x2, u0, c0, parsed, docling_artifact_sliced, 0.03 | 2/3, x2, u0, c0, parsed, docling_artifact_sliced, 0.033 | 2/3, x2, u0, c0, parsed, docling_artifact_sliced, 0.033 | 2/3, x2, u0, c0, parsed, docling_artifact_sliced, 0.038 |
| hd0179_jaa | 0/0, x0, u0, c0, empty, docling_artifact_sliced, 0.264 | 0/0, x0, u0, c0, empty, docling_artifact_sliced, 0.284 | 0/0, x0, u0, c0, empty, docling_artifact_sliced, 0.271 | 0/0, x0, u0, c0, empty, docling_artifact_sliced, 0.257 | 0/0, x0, u0, c0, empty, docling_artifact_sliced, 0.209 | 0/0, x0, u0, c0, empty, docling_artifact_sliced, 0.229 |
| hd7365_jaa | 0/0, x0, u0, c0, empty, pdfplumber, 3.775 | 0/0, x0, u0, c0, empty, pdfplumber, 4.543 | 0/0, x0, u0, c0, empty, pdfplumber, 3.59 | 0/0, x0, u0, c0, empty, pdfplumber, 3.73 | 0/0, x0, u0, c0, empty, pdfplumber, 3.144 | 0/0, x0, u0, c0, empty, pdfplumber, 3.152 |
| hd5324_jaa | 0/0, x0, u0, c0, empty, pdfplumber, 5.958 | 0/0, x0, u0, c0, empty, pdfplumber, 3.606 | 0/0, x0, u0, c0, empty, pdfplumber, 3.066 | 0/0, x0, u0, c0, empty, pdfplumber, 4.46 | 0/0, x0, u0, c0, empty, pdfplumber, 2.703 | 0/0, x0, u0, c0, empty, pdfplumber, 3.639 |
| hd1882_jaa_rev3 | 8/8, x8, u0, c0, parsed, docling_artifact_sliced, 0.017 | 8/8, x8, u0, c0, parsed, docling_artifact_sliced, 0.022 | 8/8, x8, u0, c0, parsed, docling_artifact_sliced, 0.016 | 8/8, x8, u0, c0, parsed, docling_artifact_sliced, 0.019 | 2/8, x2, u0, c0, parsed, docling_artifact_sliced, 0.011 | 8/8, x8, u0, c0, parsed, docling_artifact_sliced, 0.03 |
| hd0378_cm | 0/0, x0, u0, c0, empty, pdfplumber, 0.537 | 0/0, x0, u0, c0, empty, pdfplumber, 0.642 | 0/0, x0, u0, c0, empty, pdfplumber, 0.338 | 0/0, x0, u0, c0, empty, pdfplumber, 0.361 | 0/0, x0, u0, c0, empty, pdfplumber, 0.556 | 0/0, x0, u0, c0, empty, pdfplumber, 0.574 |
| hd0379_r28 | 0/0, x0, u0, c0, empty, docling_artifact_sliced, 0.015 | 0/0, x0, u0, c0, empty, docling_artifact_sliced, 0.013 | 0/0, x0, u0, c0, empty, docling_artifact_sliced, 0.012 | 0/0, x0, u0, c0, empty, docling_artifact_sliced, 0.019 | 0/0, x0, u0, c0, empty, docling_artifact_sliced, 0.017 | 0/0, x0, u0, c0, empty, docling_artifact_sliced, 0.015 |
| hd7770_dec_riderlc | 0/12, x0, u0, c0, empty, docling_artifact_sliced, 0.124 | 0/12, x0, u0, c0, empty, docling_artifact_sliced, 0.126 | 0/12, x0, u0, c0, empty, docling_artifact_sliced, 0.09 | 0/12, x0, u0, c0, empty, docling_artifact_sliced, 0.166 | 0/12, x0, u0, c0, empty, docling_artifact_sliced, 0.11 | 0/12, x0, u0, c0, empty, docling_artifact_sliced, 0.135 |
| hd0396_ssr | 0/0, x0, u0, c0, empty, docling_artifact_sliced, 0.032 | 0/0, x0, u0, c0, empty, docling_artifact_sliced, 0.031 | 0/0, x0, u0, c0, empty, docling_artifact_sliced, 0.024 | 0/0, x0, u0, c0, empty, docling_artifact_sliced, 0.039 | 0/0, x0, u0, c0, empty, docling_artifact_sliced, 0.018 | 0/0, x0, u0, c0, empty, docling_artifact_sliced, 0.028 |
| hd7295_car | 0/0, x0, u0, c0, skipped_order, None, 0.024 | 0/0, x0, u0, c0, skipped_order, None, 0.031 | 0/0, x0, u0, c0, skipped_order, None, 0.023 | 0/0, x0, u0, c0, skipped_order, None, 0.033 | 0/0, x0, u0, c0, skipped_order, None, 0.017 | 0/0, x0, u0, c0, skipped_order, None, 0.031 |
| hd0392_pps | 0/1, x0, u0, c0, skipped_formula, None, 0.036 | 0/1, x0, u0, c0, skipped_formula, None, 0.023 | 0/1, x0, u0, c0, skipped_formula, None, 0.016 | 0/1, x0, u0, c0, skipped_formula, None, 0.032 | 0/1, x0, u0, c0, skipped_formula, None, 0.027 | 0/1, x0, u0, c0, skipped_formula, None, 0.018 |
| hd0403_cei | 0/0, x0, u0, c0, skipped_formula, None, 0.011 | 0/0, x0, u0, c0, skipped_formula, None, 0.02 | 0/0, x0, u0, c0, skipped_formula, None, 0.011 | 0/0, x0, u0, c0, skipped_formula, None, 0.021 | 0/0, x0, u0, c0, skipped_formula, None, 0.012 | 0/0, x0, u0, c0, skipped_formula, None, 0.013 |
| hd0408_rsnes | 0/0, x0, u0, c0, empty, docling_artifact_sliced, 0.018 | 0/0, x0, u0, c0, empty, docling_artifact_sliced, 0.022 | 0/0, x0, u0, c0, empty, docling_artifact_sliced, 0.016 | 0/0, x0, u0, c0, empty, docling_artifact_sliced, 0.019 | 0/0, x0, u0, c0, empty, docling_artifact_sliced, 0.016 | 0/0, x0, u0, c0, empty, docling_artifact_sliced, 0.021 |
| hd0182_jaa_order | 0/0, x0, u0, c0, skipped_reference, None, 0.01 | 0/0, x0, u0, c0, skipped_reference, None, 0.016 | 0/0, x0, u0, c0, skipped_reference, None, 0.011 | 0/0, x0, u0, c0, skipped_reference, None, 0.015 | 0/0, x0, u0, c0, skipped_reference, None, 0.01 | 0/0, x0, u0, c0, skipped_reference, None, 0.013 |
| hd7819_edit4_term | 0/0, x0, u0, c0, empty, pdfplumber, 0.043 | 0/0, x0, u0, c0, empty, pdfplumber, 0.045 | 0/0, x0, u0, c0, empty, pdfplumber, 0.041 | 0/0, x0, u0, c0, empty, pdfplumber, 0.05 | 0/0, x0, u0, c0, empty, pdfplumber, 0.029 | 0/0, x0, u0, c0, empty, pdfplumber, 0.181 |
