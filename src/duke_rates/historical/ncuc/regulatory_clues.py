"""Page-level regulatory clue extraction (Phase 2E D1).

Pure functions: page text in, :class:`PageClue` list out. No database or PDF access.

Every clue carries the exact ``[char_start:char_end]`` slice of the page text it
was read from, so ``printed_text`` is always recoverable from the cited page.
``normalized_value`` is a machine interpretation of that text and says how it
was derived (``normalization``):

* ``exact`` / ``normalized``: an unambiguous reading.
* ``ocr_repaired``: the reading required repairing OCR or glue damage
  (``"LeafNo."``, ``"No.E-2"``, ``"June 28,2011"``).
* ``redline_ambiguous``: two values printed back-to-back (``"First RevisedOriginal"``,
  ``"Sub 12431214"``, ``"September 30, 2024December 10, 2024"``). That is the text
  layer of a redline, where the deleted and inserted values both survive. The
  candidates are kept in ``normalized_json``; ``normalized_value`` is ``None``
  because the text alone cannot say which value is in force.
* ``blank``: a printed placeholder (``"Order dated ____"``), itself evidence of
  an unissued / proposed sheet.

The patterns generalize the existing extractors so that their matches remain a
subset (see ``tests/test_regulatory_clues.py``):

* docket / order date: ``parse.heuristics.NCUC_FOOTER_RE`` (adds docket lists,
  "Orders dated A and B", missing space after "No.", blanks);
* supersedes: ``parse.heuristics.SUPERSEDES_LEAF_RE`` / ``SUPERSEDES_SCHEDULE_RE``
  (adds "North Carolina ..." which DEC prints and the old pattern cannot match);
* revision / leaf: ``parse.nc_carolinas._REVISION_RE`` and
  ``historical.metadata.REVISION_LINE_RE``;
* effective date: ``parse.nc_carolinas._EFFECTIVE_RE`` / ``_EFFECTIVE_ALT_RE`` and
  ``historical.metadata.EFFECTIVE_RANGE_RE``.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import date

EXTRACTOR_NAME = "regulatory_clues"
EXTRACTOR_VERSION = "1.1.0"  # 1.1.0: benchmark-driven fixes (blank orders, ", and" lists, (Proposed), on-or-after, roles)

# ---------------------------------------------------------------------------
# Ordinals
# ---------------------------------------------------------------------------

_UNITS = {
    "first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5, "sixth": 6,
    "seventh": 7, "eighth": 8, "ninth": 9,
}
_TEENS = {
    "tenth": 10, "eleventh": 11, "twelfth": 12, "thirteenth": 13, "fourteenth": 14,
    "fifteenth": 15, "sixteenth": 16, "seventeenth": 17, "eighteenth": 18, "nineteenth": 19,
}
_TENS = {"twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60, "seventy": 70,
         "eighty": 80, "ninety": 90}
_TENS_ORD = {"twentieth": 20, "thirtieth": 30, "fortieth": 40, "fiftieth": 50, "sixtieth": 60,
             "seventieth": 70, "eightieth": 80, "ninetieth": 90}


def _build_ordinal_table() -> dict[str, int]:
    table: dict[str, int] = {"original": 0}
    table.update(_UNITS)
    table.update(_TEENS)
    table.update(_TENS_ORD)
    for tens_word, tens in _TENS.items():
        for unit_word, unit in _UNITS.items():
            table[f"{tens_word}-{unit_word}"] = tens + unit
    return table


ORDINALS: dict[str, int] = _build_ordinal_table()
_ORDINAL_WORDS = sorted(ORDINALS, key=len, reverse=True)
# One ordinal token as printed: "Twenty-Ninth", "Twenty Ninth", "TwentyNinth", "Original".
_ORDINAL_TOKEN = (
    r"(?:(?:Twenty|Thirty|Forty|Fifty|Sixty|Seventy|Eighty|Ninety)[\s-]?"
    r"(?:First|Second|Third|Fourth|Fifth|Sixth|Seventh|Eighth|Ninth)"
    r"|Original|First|Second|Third|Fourth|Fifth|Sixth|Seventh|Eighth|Ninth|Tenth|Eleventh|Twelfth"
    r"|Thirteenth|Fourteenth|Fifteenth|Sixteenth|Seventeenth|Eighteenth|Nineteenth"
    r"|Twentieth|Thirtieth|Fortieth|Fiftieth|Sixtieth|Seventieth|Eightieth|Ninetieth)"
)
_ORDINAL_TOKEN_RE = re.compile(_ORDINAL_TOKEN, re.I)


def parse_ordinals(phrase: str) -> list[int]:
    """All ordinals printed in ``phrase``, in order ("FifthSixth Revised" -> [5, 6])."""
    values = []
    for m in _ORDINAL_TOKEN_RE.finditer(phrase):
        key = re.sub(r"[\s-]+", "", m.group(0).lower())
        for word, value in ORDINALS.items():
            if word.replace("-", "") == key:
                values.append(value)
                break
    return values


def ordinal_word(value: int) -> str:
    for word, v in ORDINALS.items():
        if v == value:
            return "-".join(p.capitalize() for p in word.split("-"))
    return str(value)


# ---------------------------------------------------------------------------
# Dates
# ---------------------------------------------------------------------------

_MONTHS = {
    m: i for i, m in enumerate(
        ["january", "february", "march", "april", "may", "june", "july", "august",
         "september", "october", "november", "december"], start=1)
}
_MONTH_ABBR = {"jan": 1, "feb": 2, "mar": 3, "apr": 4, "jun": 6, "jul": 7, "aug": 8,
               "sep": 9, "sept": 9, "oct": 10, "nov": 11, "dec": 12}
_MONTH_NAME = (
    r"(?:January|February|March|April|May|June|July|August|September|October|November|December"
    r"|Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sept|Sep|Oct|Nov|Dec)\.?"
)
# "August 18, 2023", "June 28,2011", "January 1. 2008", glued "JanuaryJuly 15, 2024", "20245".
_DATE = rf"(?:{_MONTH_NAME}){{1,2}}\s*\d{{1,3}}\s*[,.]?\s*\d{{4,5}}"
_DATE_RE = re.compile(_DATE, re.I)
_BLANK = r"_{2,}"


@dataclass(frozen=True)
class DateReading:
    iso: str | None
    normalization: str
    candidates: tuple[str, ...] = ()


def read_date(printed: str) -> DateReading:
    """Interpret one printed date; flags redline glue rather than guessing."""
    m = re.fullmatch(
        rf"\s*((?:{_MONTH_NAME}))((?:{_MONTH_NAME}))?\s*(\d{{1,3}})\s*([,.]?)\s*(\d{{4,5}})\s*",
        printed,
        re.I,
    )
    if not m:
        return DateReading(None, "unparsed")
    month1 = _month_number(m.group(1))
    month2 = _month_number(m.group(2)) if m.group(2) else None
    day_text, sep, year_text = m.group(3), m.group(4), m.group(5)
    candidates: list[str] = []
    glued = month2 is not None or len(year_text) == 5 or len(day_text) == 3
    months = [month1] + ([month2] if month2 else [])
    days = [day_text] if len(day_text) < 3 else [day_text[:-1], day_text[-2:], day_text[:1], day_text[1:]]
    years = [year_text] if len(year_text) == 4 else [year_text[:4], year_text[:3] + year_text[4]]
    for mo in months:
        for d in days:
            for y in years:
                iso = _iso(int(y), mo, int(d))
                if iso and iso not in candidates:
                    candidates.append(iso)
    if glued:
        return DateReading(None, "redline_ambiguous", tuple(candidates))
    if not candidates:
        return DateReading(None, "unparsed")
    repaired = sep != "," or not re.search(r",\s+\d{4}$", printed.strip())
    return DateReading(candidates[0], "ocr_repaired" if repaired else "exact", tuple(candidates))


def _month_number(token: str) -> int:
    t = token.lower().rstrip(".")
    return _MONTHS.get(t) or _MONTH_ABBR[t]


def _iso(y: int, mo: int, d: int) -> str | None:
    try:
        return date(y, mo, d).isoformat()
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Clue record
# ---------------------------------------------------------------------------


@dataclass
class PageClue:
    kind: str
    char_start: int
    char_end: int
    printed_text: str
    normalized_value: str | None
    normalization: str
    role: str | None = None
    normalized: dict = field(default_factory=dict)

    def normalized_json(self) -> str:
        return json.dumps(self.normalized, sort_keys=True)


# ---------------------------------------------------------------------------
# Patterns
# ---------------------------------------------------------------------------

_STATE = r"(?<![A-Za-z])(?:North\s*Carolinas?(?:\s*NC)?|South\s*Carolina|NC|SC)"
_LEAF_NUM = r"\d{1,4}[A-Z]?(?:-\d{1,3})?"
_LEAF = rf"Leaf\s*No\.?\s*(?P<leaf>{_LEAF_NUM})\b"
# Revision header: "NC Fifteenth Revised Leaf No. 99", "North Carolina (Proposed) Original Leaf No. 137",
# glued redline "NC Original First Revised Leaf No. 660".
REVISION_RE = re.compile(
    rf"(?P<state>{_STATE})\s*(?P<proposed>\(Proposed\)\s*)?"
    rf"(?P<ords>(?:{_ORDINAL_TOKEN}\s*(?:\(Proposed\)\s*)?(?:Revised\s*)?){{1,3}})" + _LEAF,
    re.I,
)
SUPERSEDES_RE = re.compile(
    r"\bSupersed(?:es|ing)\s+(?P<body>"
    rf"(?P<state>{_STATE})?\s*(?P<ords>(?:{_ORDINAL_TOKEN}\s*(?:Revised\s*)?){{1,3}})" + _LEAF +
    r"|(?:(?:NC|SC)\s+)?(?:Schedule\s+|Rider\s+|Program\s+)?(?P<code>[A-Z][A-Z0-9]*(?:-[A-Z0-9]+)*-\d+[A-Z]?)\b"
    r"|(?P<other>(?:NC|SC)\s+[^\n]{3,60}?(?:Service Regulations|Regulations)(?:\s*\(effective[^)\n]*\))?)"
    r")",
    re.I,
)
# Docket list: "E-2, Sub 1300", "E-7 Sub 1058 and M-100 Sub 138", "E-2. Sub 1300" (OCR period),
# "Subs 1356 and 1358", "Subs 1308 & 931".
_DOCKET_PHRASE_RE = re.compile(
    r"(?P<ncuc>NCUC\s+)?Docket\s*Nos?\.?\s*(?P<list>"
    r"[A-Z]{1,3}\s*-\s*\d{1,4}\s*[,.]?\s*Subs?\.?\s*\d{1,9}"
    r"(?:\s*(?:,\s*and|,|and|&)\s*(?:(?:[A-Z]{1,3}\s*-\s*\d{1,4}\s*[,.]?\s*)?Subs?\.?\s*)?\d{1,9})*)"
    r"(?P<order>\s*[,;]?\s*Orders?\s+dated\s*(?P<dates>"
    rf"{_BLANK}|{_DATE}(?:\s*(?:and|,|&)?\s*{_DATE})*"
    r"|(?!\s*\d))"  # "Order dated" with nothing after it: an unissued order
    r")?",
    re.I,
)
_STANDALONE_ORDER_RE = re.compile(rf"\bOrders?\s+dated\s*(?P<dates>{_BLANK}|{_DATE}(?:\s*(?:and|&)\s*{_DATE})*)", re.I)
EFFECTIVE_RE = re.compile(
    r"\bEffective\s+(?:(?:for\s+(?:service|services|bills?)\s+(?:rendered\s+)?)?"
    r"(?:on\s+(?:and|or)\s+after|from)\s*(?:from\s*)?|(?=[A-Z][a-z]+\.?\s*\d))"
    rf"(?P<start>{_BLANK}|{_DATE})"
    rf"(?:\s*(?:through|to)\s*(?P<end>{_BLANK}|{_DATE}))?",
)
_UTILITY_RE = re.compile(
    r"Duke\s+Energy\s+Progress|Progress\s+Energy\s+Carolinas|Carolina\s+Power\s+(?:&|and)\s+Light"
    r"|Duke\s+Energy\s+Carolinas|Duke\s+Power\s+Company",
    re.I,
)
_SCHEDULE_HEADER_RE = re.compile(
    r"\b(?:SCHEDULE|Schedule|RIDER|Rider)\s+(?P<code>[A-Z][A-Z0-9]{0,9}(?:[-/][A-Z0-9]{1,8}){0,3})\b"
)
_SCHEDULE_STOPWORDS = {
    "A", "AND", "OF", "THE", "FOR", "IS", "TO", "NO", "APPLICABILITY", "AVAILABILITY", "SERVICE",
    "ADJUSTMENTS", "SUMMARY", "RATE", "RATES", "NC", "SC", "OR", "IN", "ON", "ALL",
}
_HEADER_LINES = 8


def utility_from_text(printed: str) -> str:
    low = printed.lower()
    return "DEC" if ("carolinas" in low and "progress" not in low) or "duke power" in low else "DEP"


def canonical_docket(prefix: str, num: str, sub: str) -> str:
    return f"{prefix.upper()}-{int(num)} Sub {int(sub)}"


# ---------------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------------


def extract_page_clues(text: str) -> list[PageClue]:
    """Every regulatory clue printed on one page, with exact offsets."""
    if not text:
        return []
    clues: list[PageClue] = []
    supersedes_spans: list[tuple[int, int]] = []

    for m in SUPERSEDES_RE.finditer(text):
        clues.append(_supersedes_clue(m))
        supersedes_spans.append(m.span())

    for m in REVISION_RE.finditer(text):
        if _inside(m.start(), supersedes_spans):
            continue
        clues.extend(_revision_clues(m))

    clues.extend(_docket_clues(text))
    clues.extend(_effective_clues(text))
    clues.extend(_utility_clues(text))
    clues.extend(_schedule_clues(text, supersedes_spans))
    clues.sort(key=lambda c: (c.char_start, c.kind))
    return clues


def _inside(pos: int, spans: list[tuple[int, int]]) -> bool:
    return any(s <= pos < e for s, e in spans)


def _ordinal_reading(ords_text: str) -> tuple[int | None, str, list[int]]:
    ordinals = parse_ordinals(ords_text)
    if len(ordinals) == 1:
        glued = bool(re.search(r"[a-z][A-Z]", ords_text.strip()))
        return ordinals[0], ("ocr_repaired" if glued else "normalized"), ordinals
    return None, "redline_ambiguous", ordinals


def _revision_clues(m: re.Match) -> list[PageClue]:
    ordinal, normalization, ordinals = _ordinal_reading(m.group("ords"))
    leaf = m.group("leaf").upper()
    state = "SC" if m.group("state").upper().startswith(("SC", "SOUTH")) else "NC"
    if re.search(r"LeafNo|No\.?\d|No\d", m.group(0)) and normalization == "normalized":
        normalization = "ocr_repaired"
    redline_state = bool(re.search(r"Carolina\s*NC", m.group("state"), re.I))
    if redline_state and normalization == "normalized":
        normalization = "redline_ambiguous"
        ordinal = None
    detail = {
        "state": state,
        "leaf": leaf,
        "ordinal": ordinal,
        "ordinals_printed": ordinals,
        "proposed_marker": bool(m.group("proposed")) or "(proposed)" in m.group("ords").lower(),
    }
    rev_value = f"{state}:leaf-{leaf}:rev-{ordinal}" if ordinal is not None else None
    leaf_start = m.start("leaf")
    leaf_phrase_start = m.group(0).lower().rfind("leaf") + m.start()
    return [
        PageClue("revision", m.start(), m.end(), m.group(0), rev_value, normalization,
                 role="header", normalized=detail),
        PageClue("leaf", leaf_phrase_start, m.end("leaf"), m.string[leaf_phrase_start:m.end("leaf")],
                 f"{state}:leaf-{leaf}", "normalized", role="header",
                 normalized={"state": state, "leaf": leaf, "leaf_number_offset": leaf_start}),
    ]


def _supersedes_clue(m: re.Match) -> PageClue:
    body = m.group("body")
    if m.group("leaf"):
        ordinal, normalization, ordinals = _ordinal_reading(m.group("ords") or "")
        state_text = m.group("state") or ""
        state = "SC" if state_text.upper().startswith(("SC", "SOUTH")) else "NC"
        leaf = m.group("leaf").upper()
        if re.search(r"LeafNo|No\d", body) and normalization == "normalized":
            normalization = "ocr_repaired"
        detail = {"target": "leaf", "state": state, "leaf": leaf, "ordinal": ordinal,
                  "ordinals_printed": ordinals}
        value = f"{state}:leaf-{leaf}:rev-{ordinal}" if ordinal is not None else None
        return PageClue("supersedes", m.start(), m.end(), m.group(0), value, normalization,
                        role="supersedes", normalized=detail)
    if m.group("code"):
        code = m.group("code").upper()
        base, _, num = code.rpartition("-")
        detail = {"target": "schedule", "code": code, "base": base,
                  "number": int(re.sub(r"\D", "", num) or 0)}
        return PageClue("supersedes", m.start(), m.end(), m.group(0), f"schedule:{code}", "normalized",
                        role="supersedes", normalized=detail)
    detail = {"target": "other", "text": " ".join(body.split())}
    return PageClue("supersedes", m.start(), m.end(), m.group(0), None, "unparsed",
                    role="supersedes", normalized=detail)


def _docket_clues(text: str) -> list[PageClue]:
    clues: list[PageClue] = []
    covered: list[tuple[int, int]] = []
    for m in _DOCKET_PHRASE_RE.finditer(text):
        role = "footer" if m.group("ncuc") else "body"
        list_start = m.start("list")
        dockets = _dockets_in_list(m.group("list"), list_start)
        for docket_value, normalization, detail in dockets:
            clues.append(PageClue("docket", m.start(), m.end("list"), text[m.start():m.end("list")],
                                  docket_value, normalization, role=role, normalized=detail))
        covered.append(m.span())
        order_dates: list[DateReading] = []
        if m.group("order"):
            order_dates = _order_date_clues(text, m.start("order"), m.end("dates"), m.group("dates"),
                                            role, clues)
        good = [d for d in dockets if d[0]]
        if good and m.group("order"):
            readable = [d.iso for d in order_dates if d.iso]
            blank = not m.group("dates").strip() or re.fullmatch(_BLANK, m.group("dates").strip()) is not None
            value = "|".join(sorted({d[0] for d in good})) + "@" + (
                ",".join(readable) if readable else ("blank" if blank else "unreadable"))
            clues.append(PageClue(
                "approval_reference", m.start(), m.end(), m.group(0), value,
                "blank" if blank else ("normalized" if readable else "redline_ambiguous"),
                role=role,
                normalized={"dockets": [d[0] for d in good], "order_dates": readable, "blank_order_date": blank},
            ))
    for m in _STANDALONE_ORDER_RE.finditer(text):
        if _inside(m.start(), covered):
            continue
        _order_date_clues(text, m.start(), m.end("dates"), m.group("dates"), "body", clues)
    return clues


def _dockets_in_list(list_text: str, offset: int) -> list[tuple[str | None, str, dict]]:
    out: list[tuple[str | None, str, dict]] = []
    prefix = num = None
    for part in re.finditer(r"(?:(?P<prefix>[A-Z]{1,3})\s*-\s*(?P<num>\d{1,4})\s*(?P<sep>[,.]?)\s*)?"
                            r"(?:Subs?\.?\s*)?(?P<sub>\d{1,9})", list_text):
        ocr = False
        if part.group("prefix"):
            prefix, num = part.group("prefix"), part.group("num")
            ocr = part.group("sep") == "."
        if prefix is None:
            continue
        sub = part.group("sub")
        detail = {"prefix": prefix.upper(), "number": int(num), "sub_printed": sub}
        if len(sub) > 4:
            detail["sub_candidates"] = sorted({sub[:len(sub) // 2], sub[len(sub) // 2:],
                                               sub[:4], sub[-4:], sub[:3], sub[-3:]})
            out.append((None, "redline_ambiguous", detail))
            continue
        normalization = "ocr_repaired" if ocr or re.search(r"No\.[A-Z]", list_text) else "normalized"
        out.append((canonical_docket(prefix, num, sub), normalization, detail))
    return out


def _order_date_clues(text: str, start: int, end: int, dates_text: str, role: str,
                      sink: list[PageClue]) -> list[DateReading]:
    if not dates_text.strip() or re.fullmatch(_BLANK, dates_text.strip()):
        sink.append(PageClue("order_date", start, end, text[start:end], None, "blank", role=role,
                             normalized={"blank": True}))
        return []
    readings = []
    dates_offset = end - len(dates_text)
    matches = list(_DATE_RE.finditer(dates_text))
    adjacent = any(matches[i].end() == matches[i + 1].start() for i in range(len(matches) - 1))
    for dm in matches:
        reading = read_date(dm.group(0))
        normalization = "redline_ambiguous" if adjacent else reading.normalization
        iso = None if adjacent else reading.iso
        readings.append(DateReading(iso, normalization, reading.candidates))
        s, e = dates_offset + dm.start(), dates_offset + dm.end()
        sink.append(PageClue("order_date", s, e, text[s:e], iso, normalization, role=role,
                             normalized={"candidates": list(reading.candidates),
                                         "adjacent_glue": adjacent}))
    return readings


def _effective_clues(text: str) -> list[PageClue]:
    clues = []
    header_end, footer_start = _header_limit(text), _footer_start(text)
    for m in EFFECTIVE_RE.finditer(text):
        role = "footer" if m.start() >= footer_start or m.start() < header_end else "body"
        start_text = m.group("start")
        if re.fullmatch(_BLANK, start_text):
            clues.append(PageClue("effective_date", m.start(), m.end(), m.group(0), None, "blank",
                                  role=role, normalized={"blank": True}))
            continue
        start = read_date(start_text)
        end = read_date(m.group("end")) if m.group("end") and not re.fullmatch(_BLANK, m.group("end")) else None
        # A second date glued straight onto the first ("January 1, 2019November 16, 2019").
        tail = text[m.end():m.end() + 40]
        glued_next = re.match(_DATE, tail, re.I) is not None and not m.group("end")
        normalization = start.normalization
        iso = start.iso
        if glued_next:
            normalization, iso = "redline_ambiguous", None
        detail = {"candidates": list(start.candidates), "end": end.iso if end else None,
                  "glued_following_date": glued_next,
                  "phrase": " ".join(text[m.start():m.start("start")].split())}
        value = iso if iso and not (end and end.iso) else (f"{iso}/{end.iso}" if iso and end and end.iso else None)
        clues.append(PageClue("effective_date", m.start(), m.end(), m.group(0), value, normalization,
                              role=role, normalized=detail))
    return clues


def _utility_clues(text: str) -> list[PageClue]:
    return [
        PageClue("utility", m.start(), m.end(), m.group(0), utility_from_text(m.group(0)), "normalized",
                 role="header" if m.start() < _header_limit(text) else "body")
        for m in _UTILITY_RE.finditer(text)
    ]


def _header_limit(text: str) -> int:
    pos = 0
    for _ in range(_HEADER_LINES):
        nxt = text.find("\n", pos)
        if nxt < 0:
            return len(text)
        pos = nxt + 1
    return pos


def _footer_start(text: str, lines: int = _HEADER_LINES) -> int:
    """Offset of the first of the page's last ``lines`` non-empty lines."""
    pos = len(text.rstrip())
    for _ in range(lines):
        nxt = text.rfind("\n", 0, pos)
        if nxt < 0:
            return 0
        pos = nxt
        while pos > 0 and text[pos - 1] == "\n":
            pos -= 1
    return pos


def _schedule_clues(text: str, supersedes_spans: list[tuple[int, int]]) -> list[PageClue]:
    limit = _header_limit(text)
    clues = []
    for m in _SCHEDULE_HEADER_RE.finditer(text, 0, limit):
        if _inside(m.start(), supersedes_spans):
            continue
        code = m.group("code").upper()
        if code in _SCHEDULE_STOPWORDS or len(code) < 2:
            continue
        clues.append(PageClue("schedule_code", m.start(), m.end(), m.group(0), f"schedule:{code}",
                              "normalized", role="header", normalized={"code": code}))
    return clues


def revision_display(state: str, leaf: str, ordinal: int) -> str:
    """Printed form used in reports and gap targets: "NC Fifteenth Revised Leaf No. 99"."""
    if ordinal == 0:
        return f"{state} Original Leaf No. {leaf}"
    return f"{state} {ordinal_word(ordinal)} Revised Leaf No. {leaf}"
