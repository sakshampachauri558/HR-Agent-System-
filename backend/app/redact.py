"""The bias guard — PRD §4 F2. Ships in v1; not optional.

Strips personally-identifying and protected-attribute signals from a
resume before the evaluator agent ever sees it: name, email, phone,
address, photo references, gender markers, date of birth, nationality,
marital status. University *prestige* signals are reduced to degree +
field (the institution name is dropped, the degree phrase is kept).

Conservative by design: every substitution targets a narrow,
high-precision pattern (a labeled field, an email/phone/URL regex, a
structurally-detected name/address line) rather than deleting whole
paragraphs — over-redacting the experience/education sections would
destroy the evidence quotes the rubric depends on, which is the one
failure mode this module must never produce.

Name detection, specifically, is layered rather than a single regex,
because a name can surface in a resume in several distinct shapes:

  1. A labeled field  — "**Candidate Name:** Priya Sharma", "Name: ...",
     with or without markdown emphasis around the label.
  2. A bare header line — the first non-blank line is just the name.
  3. "Name — Title" / "Name | contact" on one line — only the name
     segment is redacted, the rest of the line survives untouched.
  4. Name immediately preceding contact info on the same line, once
     email/phone have already been turned into `[REDACTED_EMAIL]` /
     `[REDACTED_PHONE]` markers.
  5. A signature-style sign-off at the end of a line/paragraph
     ("... - Priya Sharma"), which recurs independently of the header
     (useful when a section is redacted in isolation and never saw the
     header at all).

Whichever shape finds the name first, that name's tokens are then
propagated (exact phrase, then leftover standalone tokens) through the
rest of the *same* redaction call, so a name repeated later in the body
("... - Priya Sharma" in a declaration) doesn't survive just because it
wasn't the line that structurally looked most like a header.

Deliberately NOT a fixture list: nothing here matches "Priya" or
"Sharma" literally. The rules are structural (position, label,
capitalisation, absence of digits/verbs, adjacency to contact info) so
they generalize to a resume never seen before.
"""

from __future__ import annotations

import re

from app.parse import is_heading_line

REDACTED = "[REDACTED]"

_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
_URL_RE = re.compile(r"\bhttps?://\S+")

# Candidate phone-number spans: a leading digit, then a run of digits and
# common separators, ending on a digit. We only redact a candidate span
# if it actually contains enough *digits* (>=9) to plausibly be a phone
# number -- this is what keeps short numeric mentions like "5 years" or
# a "2019-2022" date range (8 digits) untouched. Deliberately space/tab
# only (not `\s`) so a match can never cross a newline into the next
# line's own digits (e.g. an address ZIP code right below a phone line).
_PHONE_CANDIDATE_RE = re.compile(r"\+?\d[\d().\- \t]{6,}\d")
_PHONE_MIN_DIGITS = 9

# ---------------------------------------------------------------------------
# Labeled-line detection. Tolerant of markdown emphasis around the label
# ("**Address:**", "__Name:__") since that's how real resumes and our own
# fixtures write a contact-header block. `_MD_PREFIX` only eats space/tab
# and markdown decoration characters -- never `\s` -- so it can never
# cross a newline and swallow the previous line.
# ---------------------------------------------------------------------------
_MD_PREFIX = r"[ \t*_#>]{0,6}"
_MD_LABEL_WRAP = r"[*_]{0,2}"

# A labeled name field. The captured group is the raw value (still
# possibly wearing markdown emphasis, stripped by the caller) so its
# tokens can be propagated through the rest of the document.
_NAME_LABEL_RE = re.compile(
    rf"^{_MD_PREFIX}(?:candidate\s+name|full\s+name|name){_MD_LABEL_WRAP}\s*[:\-]\s*(.*)$",
    re.IGNORECASE | re.MULTILINE,
)

# A labeled address field -- always safe to replace wholesale, an address
# label never doubles as rubric evidence.
_ADDRESS_LABEL_RE = re.compile(
    rf"^{_MD_PREFIX}(?:home\s+address|address){_MD_LABEL_WRAP}\s*[:\-].*$",
    re.IGNORECASE | re.MULTILINE,
)

# Everything else that's pure identity/protected-attribute metadata when
# it opens a line as a labeled field.
_OTHER_LABELED_LINE_RE = re.compile(
    rf"^{_MD_PREFIX}(?:date of birth|dob|nationality|citizenship|marital status|"
    rf"gender|sex|photo|photograph|passport(?:\s*no\.?)?|religion)"
    rf"{_MD_LABEL_WRAP}\s*[:\-].*$",
    re.IGNORECASE | re.MULTILINE,
)

# A whole line carrying a "City, ST 12345"-shaped span is almost always a
# US-style mailing address block, not a rubric-evidence sentence.
_ADDRESS_LINE_US_RE = re.compile(r"^.*\b[A-Z]{2}\s+\d{5}(?:-\d{4})?\b.*$")

# Indian addresses don't follow the "ST 12345" shape: a 6-digit PIN code,
# often with no state abbreviation at all ("Bengaluru, Karnataka 560038").
# A bare 6-digit number is the *necessary* signal (a word like "sector" or
# "street" shows up in ordinary prose -- "the fintech sector",
# "cross-functional" -- far too often to trust alone). A locality keyword
# or a leading "45, ..." building-number is only used to *confirm* a PIN
# match, never to trigger redaction by itself.
_INDIAN_PIN_RE = re.compile(r"(?<!\d)\d{6}(?!\d)")
_ADDRESS_KEYWORD_RE = re.compile(
    r"\b(?:road|rd\.?|street|st\.?|marg|nagar|sector|colony|layout|lane|ln\.?|"
    r"flat|apartment|apt\.?|block|phase|extension|ext\.?|circle|society|"
    r"chs|pin\s*code|zip\s*code)\b",
    re.IGNORECASE,
)
_ADDRESS_LEADING_NUMBER_RE = re.compile(r"^\s*\d{1,4}[A-Za-z]?\s*,")

_PHOTO_REF_RE = re.compile(
    r"\S+\.(?:jpe?g|png|gif|bmp|heic)\b|\b(?:headshot|profile photo|photograph attached)\b",
    re.IGNORECASE,
)

_GENDER_MARKER_RE = re.compile(
    r"\b(?:he/him|she/her|they/them|mr\.?|mrs\.?|ms\.?|male|female|non-binary|nonbinary)\b",
    re.IGNORECASE,
)

# Name sharing a line with contact info: "Name  email@x.com" or
# "Name | (555) 123-4567". Fires only directly adjacent to an
# email/phone marker we just inserted, so it never touches body prose.
_NAME_BEFORE_CONTACT_RE = re.compile(
    r"^([A-Z][A-Za-z'-]*(?:\s+[A-Z][A-Za-z'-]*){0,3})[\s|,-]+(?=\[REDACTED_EMAIL\]|\[REDACTED_PHONE\])",
    re.MULTILINE,
)

# Header-line splitter for "Name — Title" / "Name | Title" / "Name - Title"
# shapes, so only the name segment is redacted and the title survives.
_HEADER_SPLIT_RE = re.compile(r"\s*\|\s*|\s+[-–—]\s+")

# A capitalized 1-4 word phrase sitting right before end-of-line, directly
# after a dash -- the classic "... - Jane Doe" signature/declaration
# sign-off. Independent of the header, so it still catches a name that
# recurs later in the document (or in a section redacted in isolation,
# where the header was never part of the text at all).
_SIGNATURE_NAME_RE = re.compile(
    r"([-–—]\s*)([A-Z][A-Za-z'-]*(?:\s+[A-Z][A-Za-z'-]*){0,3})\s*$",
    re.MULTILINE,
)

# Common resume job-title words. A first line that's actually a title
# ("Senior Backend Engineer") rather than a name must not be mistaken for
# one just because it's 1-4 capitalized words.
_TITLE_STOPWORDS = {
    "senior", "junior", "lead", "principal", "staff", "engineer", "developer",
    "manager", "director", "architect", "analyst", "consultant", "specialist",
    "officer", "president", "founder", "intern", "associate", "scientist",
    "designer", "administrator", "coordinator", "executive", "head", "chief",
    "recruiter", "engineering", "resume", "curriculum", "vitae",
}

# University-prestige reducer: strips a proper-noun institution name that
# follows (or is) a "University"/"College"/"Institute"/"Polytechnic"
# phrase, leaving any preceding degree + field text (e.g. "BSc Computer
# Science, Stanford University" -> "BSc Computer Science").
_UNIVERSITY_RE = re.compile(
    r"[,;–\-]?\s*"
    r"(?:"
    r"(?:[A-Z][\w.&'-]*\s+){0,4}(?:University|Institute of Technology|Polytechnic|College)"
    r"(?:\s+of\s+[A-Z][\w.&'-]*(?:\s+[A-Z][\w.&'-]*){0,2})?"
    r"|University\s+of\s+(?:[A-Z][\w.&'-]*\s*){1,4}"
    r")"
)


def _looks_like_name_phrase(phrase: str) -> bool:
    """Structural name test: 1-5 words, each title-case (or a bare
    initial like "Q."), no digits, no '@', and none of them a common
    job-title word. Deliberately has no knowledge of any real name."""
    phrase = phrase.strip()
    if not phrase:
        return False
    words = phrase.split()
    if not (1 <= len(words) <= 5):
        return False
    if "@" in phrase or any(ch.isdigit() for ch in phrase):
        return False
    for w in words:
        if w.strip(".,").lower() in _TITLE_STOPWORDS:
            return False
        core = w.rstrip(".").replace("-", "").replace("'", "")
        if not core or not core.isalpha() or not w[:1].isupper():
            return False
    return True


def _looks_like_address_line(line: str) -> bool:
    stripped = line.strip()
    if not stripped or stripped.startswith("[REDACTED"):
        return False
    if _ADDRESS_LINE_US_RE.match(line):
        return True
    if not _INDIAN_PIN_RE.search(line):
        return False  # PIN code is the necessary signal; nothing else stands alone
    return bool(_ADDRESS_KEYWORD_RE.search(line)) or bool(_ADDRESS_LEADING_NUMBER_RE.match(line))


def _redact_name_labeled_lines(text: str) -> tuple[str, str | None]:
    """Replace any "Name:"/"Candidate Name:"/"Full Name:" labeled line
    wholesale, and return the first plausible name value found (for
    propagation) alongside the redacted text."""
    captured: list[str] = []

    def repl(match: re.Match[str]) -> str:
        raw = match.group(1).strip().strip("*_").strip()
        if raw:
            captured.append(raw)
        return "[REDACTED_NAME]"

    new_text = _NAME_LABEL_RE.sub(repl, text)
    name = next((c for c in captured if _looks_like_name_phrase(c)), None)
    return new_text, name


def _redact_header_name(text: str) -> tuple[str, str | None]:
    """Best-effort: a resume's very first non-blank line is very often
    the candidate's name, alone or sharing a line with a title/contact
    info via a dash or pipe separator. Only that first line is ever
    touched here, so this never reaches into the body where evidence
    quotes live."""
    lines = text.splitlines()
    for i, line in enumerate(lines):
        stripped = line.strip()
        if not stripped:
            continue
        if is_heading_line(stripped):
            break
        split_match = _HEADER_SPLIT_RE.search(stripped)
        if split_match:
            head = stripped[: split_match.start()].strip()
            tail = stripped[split_match.end():]
            if _looks_like_name_phrase(head):
                lines[i] = f"[REDACTED_NAME]{split_match.group(0)}{tail}"
                return "\n".join(lines), head
        if _looks_like_name_phrase(stripped):
            lines[i] = "[REDACTED_NAME]"
            return "\n".join(lines), stripped
        break  # only the first non-blank line is ever a header candidate
    return text, None


def _redact_name_before_contact(text: str) -> tuple[str, str | None]:
    captured: list[str] = []

    def repl(match: re.Match[str]) -> str:
        name = match.group(1)
        if _looks_like_name_phrase(name):
            captured.append(name)
            return "[REDACTED_NAME] "
        return match.group(0)

    new_text = _NAME_BEFORE_CONTACT_RE.sub(repl, text)
    return new_text, (captured[0] if captured else None)


def _redact_signature_name(text: str) -> str:
    """Catch a "... - Jane Doe" sign-off at the end of a line. Runs
    independently of header/label detection so a section redacted in
    isolation (no header in view) still strips a name that only ever
    appears as a declaration/signature line."""

    def repl(match: re.Match[str]) -> str:
        dash, phrase = match.group(1), match.group(2)
        if _looks_like_name_phrase(phrase):
            return f"{dash}[REDACTED_NAME]"
        return match.group(0)

    return _SIGNATURE_NAME_RE.sub(repl, text)


def _propagate_name(text: str, name: str | None) -> str:
    """Once a name has been identified anywhere in this call's text,
    strip every other verbatim occurrence of it: the full phrase first,
    then any leftover lone tokens (so "Priya led the migration" loses
    "Priya" even where "Priya Sharma" doesn't appear together)."""
    if not name:
        return text
    tokens = [t for t in name.split() if t]
    if not tokens:
        return text

    phrase_pattern = re.compile(
        r"\b" + r"\s+".join(re.escape(t.rstrip(".")) + r"\.?" for t in tokens) + r"\b",
        re.IGNORECASE,
    )
    text = phrase_pattern.sub("[REDACTED_NAME]", text)

    for token in tokens:
        core = token.rstrip(".").replace("'", "").replace("-", "")
        if len(core) < 2:
            continue  # bare initials are too high a false-positive risk alone
        tok_pattern = re.compile(r"\b" + re.escape(token.rstrip(".")) + r"\.?\b")
        text = tok_pattern.sub("[REDACTED_NAME]", text)
    return text


def _redact_phones(text: str) -> str:
    def repl(match: re.Match[str]) -> str:
        span = match.group(0)
        digit_count = sum(ch.isdigit() for ch in span)
        return "[REDACTED_PHONE]" if digit_count >= _PHONE_MIN_DIGITS else span

    return _PHONE_CANDIDATE_RE.sub(repl, text)


def _redact_address_lines(text: str) -> str:
    return "\n".join(
        "[REDACTED_ADDRESS]" if _looks_like_address_line(line) else line
        for line in text.splitlines()
    )


def _reduce_university_prestige(text: str) -> str:
    return _UNIVERSITY_RE.sub("", text)


def redact(text: str) -> str:
    """Strip PII / protected-attribute signals from resume text.

    Order: name detection first (labeled line, else header line), then
    other labeled fields (address, dob, nationality, ...), then narrow
    inline regexes (email/url/phone), then name-before-contact and name
    propagation (so every recurrence of the detected name is caught in
    the same pass), then the signature-line fallback, then general
    address-line detection, photo/gender markers, university-prestige
    reduction, then whitespace cleanup.
    """
    if not text:
        return text or ""

    out = text
    names: list[str] = []

    out, label_name = _redact_name_labeled_lines(out)
    if label_name:
        names.append(label_name)

    if not names:
        out, header_name = _redact_header_name(out)
        if header_name:
            names.append(header_name)

    out = _ADDRESS_LABEL_RE.sub("[REDACTED_ADDRESS]", out)
    out = _OTHER_LABELED_LINE_RE.sub(REDACTED, out)

    out = _EMAIL_RE.sub("[REDACTED_EMAIL]", out)
    out = _URL_RE.sub("[REDACTED_LINK]", out)
    out = _redact_phones(out)

    out, contact_name = _redact_name_before_contact(out)
    if contact_name and contact_name not in names:
        names.append(contact_name)

    for name in names:
        out = _propagate_name(out, name)

    out = _redact_signature_name(out)
    out = _redact_address_lines(out)
    out = _PHOTO_REF_RE.sub("[REDACTED_PHOTO]", out)
    out = _GENDER_MARKER_RE.sub("", out)
    out = _reduce_university_prestige(out)

    # Whitespace cleanup left behind by removals. Newlines are preserved
    # (never collapsed into each other) so section structure survives.
    out = re.sub(r"[ \t]{2,}", " ", out)
    out = re.sub(r"[ \t]+\n", "\n", out)
    out = re.sub(r"\n{3,}", "\n\n", out)
    return out.strip()


def normalize_whitespace(text: str) -> str:
    """Collapse all whitespace runs to a single space, for the evidence
    quote substring-verification step (PRD §10)."""
    return re.sub(r"\s+", " ", text or "").strip()
