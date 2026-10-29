# Native redline regression corpus

Twenty-nine original pages from archived NCUC filings and existing D2 native
excerpts. `manifest.json` records source paths, original pages, SHA-256 values,
review observations, and the pre-D5 verdict. New excerpts use PyMuPDF
`insert_pdf`, preserving native text, vectors, annotations and image objects;
they are not rasterized. Existing D2 excerpts retain their prior provenance in
`../d2_redline/make_fixtures.py`.

Labels are **assistant rendered-page reviews**, never independently human
verified. They describe visible edit markup, not regulatory approval. The
corpus includes clean/marked counterparts in a single filing, marked rates,
identity-only edits, colored stamps, an underlined email false positive, blue
box/table decoration, negative values, and image-dominant pages with searchable
overlays. Such overlays do not expose flattened visual edits to a native
structural detector; these pages should remain insufficient evidence.

This is a small curated development/regression set, not a held-out estimate of
archive-wide accuracy. Source review used rendered images; detection uses
neither rendering nor OCR. Synthetic geometric controls remain separate in
`tests/test_d5_native_redline.py`.

Run `bench/redline_native.py --out PATH` using the project Python environment.
It checks fixture hashes and reports abstentions separately from binary
classification errors. No database connection or network retrieval is used.
