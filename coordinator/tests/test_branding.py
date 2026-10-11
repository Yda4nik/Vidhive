"""App icon: shown in the header and as the browser-tab favicon."""

import struct

PNG_SIG = b"\x89PNG\r\n\x1a\n"


def test_favicon_is_public_and_a_real_multi_size_ico(anon_client):
    """Browsers request /favicon.ico by themselves, before anyone has logged in."""
    r = anon_client.get("/favicon.ico", follow_redirects=False)
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("image/")
    assert r.content[:4] == b"\x00\x00\x01\x00"                  # ICO header
    count = struct.unpack("<H", r.content[4:6])[0]
    sizes = sorted(r.content[6 + 16 * i] or 256 for i in range(count))
    assert sizes == [16, 32, 48]


def test_icon_assets_are_served_without_login(anon_client):
    for path in ("/static/logo.png", "/static/favicon-32.png", "/static/apple-touch-icon.png"):
        r = anon_client.get(path, follow_redirects=False)
        assert r.status_code == 200, path
        assert r.content.startswith(PNG_SIG), path


def test_pages_declare_the_tab_icon_and_show_the_header_logo(client):
    for page in ("/", "/library", "/jobs"):
        html = client.get(page).text
        assert 'rel="icon" href="/favicon.ico' in html, page
        assert 'rel="apple-touch-icon"' in html, page
        assert 'class="brand-logo"' in html, page
        assert "🐝" not in html, page                              # the old emoji is gone


def test_login_page_also_has_the_icon(anon_client):
    html = anon_client.get("/login").text
    assert 'rel="icon"' in html and 'class="brand-logo"' in html


def test_csp_still_allows_the_logo_mask(client):
    """The header logo is a CSS mask-image from our own origin (governed by img-src)."""
    csp = client.get("/").headers["content-security-policy"]
    assert "img-src 'self'" in csp


def test_hidden_attribute_always_wins_over_author_display_rules(client):
    """Regression: `.vh-overlay{display:flex}` overrode `hidden`, so the closed dialog's
    full-screen overlay silently covered every page and swallowed all clicks."""
    import re

    html = client.get("/").text
    assert re.search(r"\[hidden\]\s*\{\s*display:\s*none\s*!important", html)
    # ...and the overlay really is the kind of rule that needs it:
    assert ".vh-overlay" in html and "display:flex" in html
