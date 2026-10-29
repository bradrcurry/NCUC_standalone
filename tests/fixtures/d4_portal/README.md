# D4 portal evidence

`docket_inventory_e7_1026_page1.html` through `page6.html` are the six actual
DocketDetails inventory pages observed on 2026-10-29 through the existing
authenticated browser session. Source capture:
`reports/phase2e_d4/browser_validation/browser/`. Page 2 was first isolated while
debugging disabled confidential detail links in the earlier transport probe.

- Docket: E-7 Sub 1026, `8903f042-4167-4b27-971e-be19e44b0240`.
- 100 visible entries, including 21 typed Order and 11 marked confidential.
  Those are page 2 counts. All six pages hold 568 entries: five pages of 100
  and a final page of 68; 85 orders total, of which two are restricted.
- The 11 confidential entries have disabled detail anchors without `href`, but
  the inventory still displays attachment references. Those references are
  retained solely as listing evidence. No confidential file was requested.
- This includes the public October 25, 2013 “Order Approving Rate Schedules”,
  document `dfcd0894-add8-4504-9d42-b8e9d0cb26ee`, beyond the original first page.
- Page 3 includes the September 24, 2013 “Order Granting General Rate Increase”,
  `413b681f-f9ba-4bfe-9b73-a410602aaffb`. No order PDF was downloaded.
- Hidden input values and script bodies were blanked. No cookies or credentials
  are stored. Trailing formatting whitespace was removed after HTML
  reserialization. This fixture is source evidence, not human review or approval.
- Synthetic sequences are explicitly derived from the earlier D3 page 1 in
  `test_d4_inventory_pagination.py`, separately from these real captures.
