from duke_rates.db.repository import Repository


def test_decode_provenance_notes_accepts_legacy_object_payload() -> None:
    payload = '{"source":"ncuc_portal_playwright","docket":"E-2 Sub 1294"}'
    notes = Repository._decode_provenance_notes(payload)
    assert len(notes) == 1
    assert '"source": "ncuc_portal_playwright"' in notes[0]
