"""Section-aware chunking for policy documents (F1, PRD §10).

Pipeline: split on markdown headings and numbered clauses FIRST (these are
the atomic units that must never be broken), then pack consecutive units
into ~800-token chunks with ~100-token overlap. A single oversized unit is
still never split -- "never split mid-clause" beats "hit the token target".

`start_char`/`end_char` on every emitted `Chunk` are offsets into the
*original* document text passed to `chunk_document`, pointing at the real
underlying clause/paragraph content (not at the synthetic section-heading
prefix added to `text`) -- that's what the F1 citation drawer highlights,
so they must slice back to exactly the quoted content.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# --- tunables ---------------------------------------------------------------

TARGET_TOKENS = 800
OVERLAP_TOKENS = 100
# A single split unit (clause/paragraph) larger than this gets a further,
# safe (sentence-boundary) split so one giant paragraph doesn't become one
# giant, budget-blowing chunk. Never applied to numbered-clause units smaller
# than this multiple, and never splits *inside* a sentence.
_OVERSIZE_MULTIPLE = 1.5

DEFAULT_SECTION = "Document"

# --- token estimation --------------------------------------------------------
# Deliberately dependency-free: chunking is pure text logic and must not load
# the (comparatively heavy) FastEmbed ONNX model just to estimate size. ~4
# chars/token is the standard rule-of-thumb for English; word-count * 1.3
# approximates BPE/WordPiece subword splitting. We blend both and round --
# good enough to hit "~800 tokens", not meant to match any specific
# tokenizer exactly.

_WORD_RE = re.compile(r"\w+|[^\w\s]")


def estimate_tokens(text: str) -> int:
    """Rough, fast token-count estimate. Not tied to any specific model's
    tokenizer -- used only to decide chunk-packing boundaries and to
    populate `Chunk.token_count`."""
    if not text:
        return 0
    words = len(_WORD_RE.findall(text))
    by_words = words * 1.3
    by_chars = len(text) / 4
    return max(1, round((by_words + by_chars) / 2))


# --- data shape ---------------------------------------------------------------


@dataclass(frozen=True)
class Chunk:
    section: str
    ordinal: int
    text: str
    start_char: int
    end_char: int
    token_count: int


@dataclass(frozen=True)
class _Unit:
    """One atomic, never-to-be-split piece of text (a numbered clause, a
    paragraph, or -- as a last resort for an oversized paragraph -- a
    sentence). `start`/`end` are offsets into the original document."""

    text: str
    start: int
    end: int
    tokens: int


# --- step 1: heading segmentation --------------------------------------------

_HEADING_RE = re.compile(r"^(#{1,6})[ \t]+(.+?)[ \t]*$", re.MULTILINE)


def _segment_sections(text: str, default_section: str) -> list[tuple[str, int, int]]:
    """Split the document into (section_label, body_start, body_end) spans.
    `body_start`/`body_end` exclude the heading line itself -- the heading
    text becomes the section label, not part of any unit's offsets."""
    matches = list(_HEADING_RE.finditer(text))
    if not matches:
        return [(default_section, 0, len(text))]

    sections: list[tuple[str, int, int]] = []
    stack: list[tuple[int, str]] = []  # (level, heading text) breadcrumb

    if matches[0].start() > 0 and text[: matches[0].start()].strip():
        sections.append((default_section, 0, matches[0].start()))

    for idx, m in enumerate(matches):
        level = len(m.group(1))
        heading_text = m.group(2).strip()
        while stack and stack[-1][0] >= level:
            stack.pop()
        stack.append((level, heading_text))
        label = " > ".join(t for _, t in stack)

        nl = text.find("\n", m.end())
        body_start = nl + 1 if nl != -1 else len(text)
        body_end = matches[idx + 1].start() if idx + 1 < len(matches) else len(text)
        sections.append((label, body_start, body_end))

    return sections


# --- step 2: clause / paragraph / sentence splitting -------------------------

# Numbered / lettered clause markers at the start of a line: "1.", "1.1)",
# "(a)", "(3)", "a)". This is what "never split mid-clause" protects.
_CLAUSE_RE = re.compile(
    r"^[ \t]*(?:\d+(?:\.\d+)*[.)]|\([a-zA-Z0-9]+\)|[a-zA-Z][.)])[ \t]+",
    re.MULTILINE,
)

_PARA_BREAK_RE = re.compile(r"\n[ \t]*\n+")

_SENTENCE_BOUNDARY_RE = re.compile(r"(?<=[.!?])[\"')\]]?\s+(?=[A-Z0-9\"'(])")


def _tight_span(raw: str, start: int, end: int) -> tuple[str, int, int] | None:
    """Trim leading/trailing whitespace from `raw[start:end]`, returning the
    trimmed text plus the tightened (start, end) -- or None if it's blank."""
    piece = raw[start:end]
    stripped = piece.strip()
    if not stripped:
        return None
    lead = len(piece) - len(piece.lstrip())
    trail = len(piece) - len(piece.rstrip())
    return stripped, start + lead, end - trail


def _split_paragraphs(body: str) -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = []
    pos = 0
    for m in _PARA_BREAK_RE.finditer(body):
        if m.start() > pos:
            spans.append((pos, m.start()))
        pos = m.end()
    if pos < len(body):
        spans.append((pos, len(body)))
    return spans or [(0, len(body))]


def _split_sentences(text: str) -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = []
    pos = 0
    for m in _SENTENCE_BOUNDARY_RE.finditer(text):
        spans.append((pos, m.start()))
        pos = m.end()
    spans.append((pos, len(text)))
    return [(s, e) for s, e in spans if text[s:e].strip()]


def _units_from_span(body: str, base_offset: int, start: int, end: int, target_tokens: int) -> list[_Unit]:
    """Build one or more `_Unit`s from `body[start:end]`, splitting further
    on sentence boundaries only if the whole span is well over budget."""
    tightened = _tight_span(body, start, end)
    if tightened is None:
        return []
    stripped, real_start_local, _real_end_local = tightened
    real_start = base_offset + real_start_local

    tokens = estimate_tokens(stripped)
    if tokens <= target_tokens * _OVERSIZE_MULTIPLE:
        return [_Unit(text=stripped, start=real_start, end=real_start + len(stripped), tokens=tokens)]

    units: list[_Unit] = []
    for s_start, s_end in _split_sentences(stripped):
        piece = stripped[s_start:s_end]
        piece_stripped = piece.strip()
        if not piece_stripped:
            continue
        lead = piece.find(piece_stripped)
        units.append(
            _Unit(
                text=piece_stripped,
                start=real_start + s_start + lead,
                end=real_start + s_start + lead + len(piece_stripped),
                tokens=estimate_tokens(piece_stripped),
            )
        )
    return units or [_Unit(text=stripped, start=real_start, end=real_start + len(stripped), tokens=tokens)]


def _split_into_units(body: str, base_offset: int, target_tokens: int) -> list[_Unit]:
    matches = list(_CLAUSE_RE.finditer(body))
    raw_spans: list[tuple[int, int]]
    if matches:
        raw_spans = []
        first_start = matches[0].start()
        if body[:first_start].strip():
            raw_spans.append((0, first_start))
        for idx, m in enumerate(matches):
            span_start = m.start()
            span_end = matches[idx + 1].start() if idx + 1 < len(matches) else len(body)
            raw_spans.append((span_start, span_end))
    else:
        raw_spans = _split_paragraphs(body)

    units: list[_Unit] = []
    for start, end in raw_spans:
        units.extend(_units_from_span(body, base_offset, start, end, target_tokens))
    return units


# --- step 3: pack units into ~target_tokens chunks with overlap -------------


def _pack_units(units: list[_Unit], target_tokens: int, overlap_tokens: int) -> list[tuple[int, int]]:
    """Return (start_idx, end_idx) [exclusive] index ranges into `units`.
    A unit is never split; a lone unit larger than `target_tokens` still
    becomes its own chunk rather than being broken up."""
    n = len(units)
    ranges: list[tuple[int, int]] = []
    i = 0
    while i < n:
        j = i
        total = 0
        while j < n:
            t = units[j].tokens
            if j > i and total + t > target_tokens:
                break
            total += t
            j += 1
        ranges.append((i, j))
        if j >= n:
            break

        # Walk back from the end of this chunk to find where ~overlap_tokens
        # worth of trailing whole units begins; that's where the next chunk
        # starts, so chunks share context instead of hard-cutting.
        k = j - 1
        back_total = 0
        while k > i and back_total < overlap_tokens:
            back_total += units[k].tokens
            k -= 1
        next_i = k + 1
        i = next_i if next_i > i else i + 1
    return ranges


# --- public API ---------------------------------------------------------------


def chunk_document(
    text: str,
    *,
    title: str | None = None,
    target_tokens: int = TARGET_TOKENS,
    overlap_tokens: int = OVERLAP_TOKENS,
) -> list[Chunk]:
    """Chunk a policy document's plain text (already extracted from PDF/MD/TXT).

    Returns chunks in document order with a global `ordinal`. Each chunk's
    `text` is prefixed with its section heading (breadcrumb of nested
    markdown headings, e.g. "Leave Policy > 1. Casual Leave") so retrieval
    sees the context; `start_char`/`end_char` point at the real clause/
    paragraph span in `text` (excluding the synthetic heading prefix), which
    is what the citation drawer highlights.
    """
    if not text or not text.strip():
        return []

    default_section = (title or DEFAULT_SECTION).strip() or DEFAULT_SECTION
    sections = _segment_sections(text, default_section)

    chunks: list[Chunk] = []
    ordinal = 0
    for section_label, body_start, body_end in sections:
        body = text[body_start:body_end]
        units = _split_into_units(body, body_start, target_tokens)
        if not units:
            continue

        for start_idx, end_idx in _pack_units(units, target_tokens, overlap_tokens):
            piece_units = units[start_idx:end_idx]
            start_char = piece_units[0].start
            end_char = piece_units[-1].end
            content = text[start_char:end_char]
            chunk_text = f"{section_label}\n\n{content}" if section_label else content
            chunks.append(
                Chunk(
                    section=section_label,
                    ordinal=ordinal,
                    text=chunk_text,
                    start_char=start_char,
                    end_char=end_char,
                    token_count=estimate_tokens(chunk_text),
                )
            )
            ordinal += 1

    return chunks
