"""Resume parsing — PDF/MD/TXT -> raw text + a tolerant section split.

Owned by A5 (F2 Evaluator). `pypdf` handles PDF; `.md`/`.txt` (and anything
else) are decoded as plain utf-8 text. `split_sections` is a heading-driven
splitter — no LLM involved, so it costs nothing against the daily budget
and runs synchronously in the upload request path.
"""

from __future__ import annotations

import io
import re

import pypdf

# Canonical section keys -> heading aliases we tolerate (lowercase, no
# punctuation). Longest alias wins so "professional experience" is matched
# before the bare "experience" prefix.
SECTION_ALIASES: dict[str, tuple[str, ...]] = {
    "summary": (
        "professional summary",
        "executive summary",
        "career summary",
        "summary",
        "profile",
        "objective",
        "about me",
    ),
    "experience": (
        "professional experience",
        "work experience",
        "employment history",
        "relevant experience",
        "career history",
        "work history",
        "experience",
    ),
    "education": (
        "education & qualifications",
        "academic qualifications",
        "academic background",
        "education",
        "qualifications",
    ),
    "skills": (
        "technical skills",
        "core competencies",
        "skills & tools",
        "key skills",
        "tech stack",
        "competencies",
        "skills",
    ),
    "projects": (
        "selected projects",
        "personal projects",
        "side projects",
        "key projects",
        "projects",
    ),
}

_HEADING_MAX_LEN = 60
_ALL_ALIASES: list[tuple[str, str]] = [
    (alias, section) for section, aliases in SECTION_ALIASES.items() for alias in aliases
]
# Longest alias first so a more specific phrase matches before a shorter
# prefix of it (e.g. "professional experience" before "experience").
_ALL_ALIASES.sort(key=lambda pair: len(pair[0]), reverse=True)


def extract_text(file_name: str, data: bytes) -> str:
    """Dispatch on file extension. `.pdf` -> pypdf; anything else -> utf-8
    text (covers `.md`, `.txt`, and any unlabeled plain-text upload)."""
    name = file_name or ""
    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    if ext == "pdf":
        return _extract_pdf_text(data)
    return data.decode("utf-8", errors="replace")


def _extract_pdf_text(data: bytes) -> str:
    reader = pypdf.PdfReader(io.BytesIO(data))
    pages: list[str] = []
    for page in reader.pages:
        try:
            pages.append(page.extract_text() or "")
        except Exception:  # noqa: BLE001 - one bad page shouldn't fail the whole upload
            pages.append("")
    return "\n".join(pages)


def _normalize_heading_line(line: str) -> str | None:
    """Return the canonical section key if `line` looks like a heading for
    one of `SECTION_ALIASES`, else None. Tolerant of markdown `#`/`##`,
    bold/underline markup, a trailing colon, and case."""
    stripped = line.strip()
    if not stripped or len(stripped) > _HEADING_MAX_LEN:
        return None
    stripped = re.sub(r"^#{1,6}\s*", "", stripped)
    stripped = stripped.strip("*_# \t")
    stripped = stripped.rstrip(":").strip()
    if not stripped:
        return None
    lower = re.sub(r"[^a-z0-9 &]", "", stripped.lower()).strip()
    if not lower:
        return None
    for alias, section in _ALL_ALIASES:
        if lower == alias or lower.startswith(alias + " "):
            return section
    return None


def is_heading_line(line: str) -> bool:
    """True if `line` is recognized as one of our section headings. Used by
    `redact.py` so the header-name heuristic never mistakes a section
    heading for a candidate name."""
    return _normalize_heading_line(line) is not None


def split_sections(text: str) -> dict[str, str]:
    """Tolerant heading matcher. Text before the first recognized heading,
    and any text under an unrecognized heading, is bucketed under "other".
    Returns only non-empty sections."""
    buckets: dict[str, list[str]] = {}
    current = "other"
    for raw_line in (text or "").splitlines():
        section = _normalize_heading_line(raw_line)
        if section is not None:
            current = section
            buckets.setdefault(current, [])
            continue
        buckets.setdefault(current, []).append(raw_line)
    return {
        key: joined
        for key, lines in buckets.items()
        if (joined := "\n".join(lines).strip())
    }
