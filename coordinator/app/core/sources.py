"""Download sources and how each maps to a target URL template.

The source is chosen per job in the UI ("auto" is resolved to a concrete source
at creation). Modes each source supports:
  - id / range : numeric identifiers -> a "{id}" template (kinescope, mock)
  - link       : full URLs (kinescope, and youtube once wired)
"""

MOCK_TEMPLATE = "http://mock:8090/{id}"
KINESCOPE_TEMPLATE = "https://kinescope.io/{id}"

TEMPLATES = {
    "kinescope": KINESCOPE_TEMPLATE,
    "mock": MOCK_TEMPLATE,
    # "youtube": <prepared, link-only, not yet wired>
}

# Which input modes each source accepts (used to block impossible combinations).
SUPPORTED_MODES = {
    "kinescope": {"id", "range", "link"},
    "mock": {"id", "range"},
    "youtube": {"link"},
}

NUMERIC_SOURCES = {"kinescope", "mock"}


def template_for(source: str) -> str:
    return TEMPLATES.get(source, KINESCOPE_TEMPLATE)


def source_from_url(url: str) -> str | None:
    """Resolve an 'auto' link to a concrete source by its host."""
    u = url.lower()
    if "youtube.com" in u or "youtu.be" in u:
        return "youtube"
    if "kinescope" in u:
        return "kinescope"
    return None
