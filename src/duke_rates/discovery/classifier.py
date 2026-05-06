from __future__ import annotations

import re

_REV_RE = re.compile(r'[?&]rev=([a-f0-9]+)', re.I)


def extract_rev_token(url: str) -> str | None:
    """Extract the ?rev= cache-busting token from a Duke CDN URL."""
    m = _REV_RE.search(url)
    return m.group(1) if m else None


def classify_document_url(
    url: str,
    *,
    state: str | None = None,
    company: str | None = None,
) -> dict[str, str | None]:
    """
    Classify a document URL into tariff_identifier, schedule_code, and rev_token.
    Uses filename patterns only — no PDF reads required.
    All values may be None if the pattern is not recognized.
    """
    from urllib.parse import urlparse
    parsed = urlparse(url)
    path = parsed.path.lower()
    filename = path.rsplit('/', 1)[-1]
    stem = re.sub(r'\.pdf$', '', filename)

    rev_token = extract_rev_token(url)
    tariff_identifier: str | None = None
    schedule_code: str | None = None

    # NC Progress / SC Progress: leaf-no-NNN-...
    m = re.search(r'leaf-no-(\d+)', stem)
    if m:
        tariff_identifier = f"leaf-{m.group(1)}"
        m2 = re.search(r'leaf-no-\d+-(?:schedule-)?([a-z][a-z0-9\-]*)', stem)
        if m2:
            schedule_code = m2.group(1).upper().replace('-', '_')
        return {"tariff_identifier": tariff_identifier, "schedule_code": schedule_code, "rev_token": rev_token}

    # NC/SC Carolinas: dec-nc-leaf-NNN or dec-nc-rider-CODE or dec-sc-*
    m = re.search(r'dec-(?:nc|sc)-leaf-(\d+)', stem)
    if m:
        tariff_identifier = f"leaf-{m.group(1)}"
        m2 = re.search(r'dec-(?:nc|sc)-leaf-\d+-(.+)', stem)
        if m2:
            schedule_code = m2.group(1).upper().replace('-', '_')[:30]
        return {"tariff_identifier": tariff_identifier, "schedule_code": schedule_code, "rev_token": rev_token}

    m = re.search(r'dec-(?:nc|sc)-rider-([a-z][a-z0-9\-]*)', stem)
    if m:
        schedule_code = m.group(1).upper().replace('-', '_')
        tariff_identifier = f"rider-{schedule_code}"
        return {"tariff_identifier": tariff_identifier, "schedule_code": schedule_code, "rev_token": rev_token}

    # FL: pe-rates-CS-NNN (schedule code is the CS segment)
    m = re.search(r'pe-rates-([a-z][a-z0-9]*)-(\d+)', stem)
    if m:
        schedule_code = m.group(1).upper()
        tariff_identifier = f"pe-{schedule_code}-{m.group(2)}"
        return {"tariff_identifier": tariff_identifier, "schedule_code": schedule_code, "rev_token": rev_token}

    # FL: def-fb-NNN, def-fcf-NNN, ce-NNN, meb-NNN patterns
    m = re.search(r'^(def-[a-z]+|ce|meb)-(\d+)', stem)
    if m:
        schedule_code = m.group(1).upper().replace('-', '_')
        tariff_identifier = f"{m.group(1)}-{m.group(2)}"
        return {"tariff_identifier": tariff_identifier, "schedule_code": schedule_code, "rev_token": rev_token}

    # IN: NNN-de-in-tariff-no-NN-name
    m = re.search(r'de-in-tariff-no-(\d+)', stem)
    if m:
        tariff_identifier = f"tariff-{m.group(1)}"
        m2 = re.search(r'de-in-tariff-no-\d+(?:-\d+)?-(.+)', stem)
        if m2:
            schedule_code = m2.group(1).upper().replace('-', '_')[:30]
        return {"tariff_identifier": tariff_identifier, "schedule_code": schedule_code, "rev_token": rev_token}

    # IN: NNN-de-in-rider-NN-CODE
    m = re.search(r'de-in-rider-(\d+)-([a-z0-9\-]+)', stem)
    if m:
        tariff_identifier = f"rider-{m.group(1)}"
        schedule_code = m.group(2).upper().replace('-', '_')
        return {"tariff_identifier": tariff_identifier, "schedule_code": schedule_code, "rev_token": rev_token}

    # IN: de-in-rider-CODE (no number)
    m = re.search(r'de-in-rider-([a-z][a-z0-9\-]+)', stem)
    if m:
        schedule_code = m.group(1).upper().replace('-', '_')
        tariff_identifier = f"rider-{schedule_code}"
        return {"tariff_identifier": tariff_identifier, "schedule_code": schedule_code, "rev_token": rev_token}

    # IN: NNN-de-in-rate-CODE (rate schedules without tariff-no)
    m = re.search(r'de-in-rate-([a-z][a-z0-9\-]+)', stem)
    if m:
        schedule_code = m.group(1).upper().replace('-', '_')
        tariff_identifier = f"rate-{schedule_code}"
        return {"tariff_identifier": tariff_identifier, "schedule_code": schedule_code, "rev_token": rev_token}

    # KY: sheet-no-NNN-ky-e-* or sheet-no-NNN-electric-ky-*
    m = re.search(r'sheet-no-(\d+)', stem)
    if m:
        tariff_identifier = f"sheet-{m.group(1)}"
        m2 = re.search(r'(?:ky|oh)-e-(.+)', stem)
        if not m2:
            m2 = re.search(r'electric-(?:ky|oh)-(.+)', stem)
        if m2:
            schedule_code = m2.group(1).upper().replace('-', '_')[:30]
        return {"tariff_identifier": tariff_identifier, "schedule_code": schedule_code, "rev_token": rev_token}

    # IN/OH/KY: tariff-no-NN (without de-in prefix, e.g. older patterns)
    m = re.search(r'tariff-no-(\d+)', stem)
    if m:
        tariff_identifier = f"tariff-{m.group(1)}"
        return {"tariff_identifier": tariff_identifier, "schedule_code": None, "rev_token": rev_token}

    # NC/SC Carolinas: {state}schedule{CODE}.pdf — e.g. ncschedulers.pdf → RS
    # Also nc-schedule-hlf.pdf
    m = re.search(r'^(?:nc|sc)-?schedule-?([a-z][a-z0-9\-]*?)(?:-tc|-ev|-tou)?$', stem)
    if m:
        schedule_code = m.group(1).upper().replace('-', '_')
        tariff_identifier = f"schedule-{schedule_code}"
        return {"tariff_identifier": tariff_identifier, "schedule_code": schedule_code, "rev_token": rev_token}

    # NC/SC Carolinas: ncrider{CODE}.pdf or scrider{CODE}.pdf (no separator)
    m = re.search(r'^(?:nc|sc)rider([a-z][a-z0-9]*?)(?:edit\d+)?$', stem)
    if m:
        schedule_code = m.group(1).upper()
        tariff_identifier = f"rider-{schedule_code}"
        return {"tariff_identifier": tariff_identifier, "schedule_code": schedule_code, "rev_token": rev_token}

    # NC/SC Carolinas: nc-rider-CODE or sc-dec-rider-CODE
    m = re.search(r'^(?:nc|sc)(?:-dec)?-rider-([a-z][a-z0-9\-]*)', stem)
    if m:
        schedule_code = m.group(1).upper().replace('-', '_')
        tariff_identifier = f"rider-{schedule_code}"
        return {"tariff_identifier": tariff_identifier, "schedule_code": schedule_code, "rev_token": rev_token}

    # NC/SC Carolinas: ncfuelcostadjrdr, nccarbonoffset, nccpre etc — named docs without code
    m = re.search(r'^(?:nc|sc)((?:fuel|carbon|cpre|adj)[a-z0-9]+)', stem)
    if m:
        schedule_code = m.group(1).upper()
        tariff_identifier = f"doc-{schedule_code}"
        return {"tariff_identifier": tariff_identifier, "schedule_code": schedule_code, "rev_token": rev_token}

    # SC Carolinas: scadjforfuelcostsderp etc
    m = re.search(r'^scadj([a-z0-9]+)', stem)
    if m:
        schedule_code = m.group(1).upper()
        tariff_identifier = f"doc-SC_ADJ_{schedule_code}"
        return {"tariff_identifier": tariff_identifier, "schedule_code": schedule_code, "rev_token": rev_token}

    # KY/OH: sheetnoNNN... (no dash — older compressed naming)
    m = re.search(r'^sheetno(\d+)', stem)
    if m:
        tariff_identifier = f"sheet-{m.group(1)}"
        return {"tariff_identifier": tariff_identifier, "schedule_code": None, "rev_token": rev_token}

    # OH older format: ohNNjanNNrNtitle etc — tariff overhead, mark with state prefix
    m = re.search(r'^oh\d+[a-z]+\d+', stem)
    if m:
        tariff_identifier = f"oh-overhead-{stem[:20]}"
        return {"tariff_identifier": tariff_identifier, "schedule_code": None, "rev_token": rev_token}

    # FL: pe-rates-WORD (no trailing number — e.g. pe-rates-sol-shared-solar)
    m = re.search(r'^pe-rates-([a-z][a-z0-9\-]+)$', stem)
    if m:
        schedule_code = m.group(1).upper().replace('-', '_')
        tariff_identifier = f"pe-{schedule_code}"
        return {"tariff_identifier": tariff_identifier, "schedule_code": schedule_code, "rev_token": rev_token}

    return {"tariff_identifier": None, "schedule_code": None, "rev_token": rev_token}
