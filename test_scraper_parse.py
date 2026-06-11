"""
Offline unit tests for the browser-free (lxml) timeline parsing added in the
Scrapling-inspired refactor. These exercise `_parse_article` and friends against
synthetic HTML that mirrors X's article DOM — no browser, no network required.

Run:  source venv/bin/activate && python3 test_scraper_parse.py
"""
import scraper


def _article(*, text_html, status="/Mr_Derivatives/status/1790000000000000001",
             datetime_attr="2026-05-31T14:03:00.000Z", social=None,
             aria="12 replies, 34 reposts, 567 likes, 8901 views",
             show_more=False, with_text_node=True):
    """Build a synthetic <article> roughly shaped like X's timeline DOM."""
    social_html = (
        f'<div data-testid="socialContext"><span>{social}</span></div>'
        if social else ""
    )
    text_node = (
        f'<div data-testid="tweetText" dir="auto">{text_html}</div>'
        if with_text_node else ""
    )
    show_more_html = (
        '<button data-testid="tweet-text-show-more-link">Show more</button>'
        if show_more else ""
    )
    aria_html = f'<div role="group" aria-label="{aria}"></div>' if aria else ""
    return f"""
    <article data-testid="tweet">
      {social_html}
      <a href="{status}"><time datetime="{datetime_attr}">May 31</time></a>
      {text_node}
      {show_more_html}
      {aria_html}
    </article>
    """


def test_engagement_label_parsing():
    out = scraper._parse_engagement_label(
        "12 replies, 34 reposts, 567 likes, 8901 views")
    assert out == {"replies": 12, "reposts": 34, "likes": 567, "views": 8901}


def test_engagement_label_with_suffixes():
    out = scraper._parse_engagement_label("1.2K reposts, 3.4M likes, 5 views")
    assert out["reposts"] == 1200
    assert out["likes"] == 3_400_000
    assert out["views"] == 5
    assert out["replies"] is None


def test_engagement_label_empty():
    assert scraper._parse_engagement_label(None) == {
        "replies": None, "reposts": None, "likes": None, "views": None}


def test_parse_article_basic():
    html = _article(text_html='Watching $NVDA closely today')
    meta = scraper._parse_article(html)
    assert meta["has_text_node"] is True
    assert meta["text"] == "Watching $NVDA closely today"
    assert meta["url"] == "https://x.com/Mr_Derivatives/status/1790000000000000001"
    assert meta["posted_at"] == "2026-05-31T14:03:00.000Z"
    assert meta["is_repost"] is False
    assert meta["has_show_more"] is False
    assert meta["engagement"]["likes"] == 567


def test_parse_article_cashtag_link_and_emoji():
    # X renders cashtags as <a> and emoji as <img alt="…">; both must survive.
    text_html = 'Loading up on <a href="/search?q=%24TSLA">$TSLA</a> <img alt="🚀" src="x.png"/>'
    meta = scraper._parse_article(_article(text_html=text_html))
    assert "$TSLA" in meta["text"]
    assert "🚀" in meta["text"]


def test_parse_article_newline_from_br():
    meta = scraper._parse_article(_article(text_html='line one<br/>line two'))
    assert meta["text"] == "line one\nline two"


def test_parse_article_repost_flagged():
    meta = scraper._parse_article(_article(
        text_html='Great thread', social="Mr_Derivatives reposted"))
    assert meta["is_repost"] is True


def test_parse_article_pinned_is_not_repost():
    # Pinned posts carry a socialContext too, but must NOT be treated as reposts.
    meta = scraper._parse_article(_article(
        text_html='Pinned thesis', social="Pinned"))
    assert meta["is_repost"] is False


def test_parse_article_show_more_detected():
    meta = scraper._parse_article(_article(
        text_html='A very long thread that is truncated', show_more=True))
    assert meta["has_show_more"] is True


def test_parse_article_no_text_node():
    # Ads / "who to follow" cards have no tweetText — must be skippable.
    meta = scraper._parse_article(_article(
        text_html='', with_text_node=False))
    assert meta["has_text_node"] is False


def test_parse_article_url_normalized_absolute():
    meta = scraper._parse_article(_article(
        text_html='hi', status="https://x.com/foo/status/123"))
    assert meta["url"] == "https://x.com/foo/status/123"


def test_parse_article_malformed_returns_empty():
    # Garbage in → {} so the caller falls back to the async path (never raises).
    assert scraper._parse_article("\x00not html<<<") == {} or \
        scraper._parse_article("\x00not html<<<").get("has_text_node") in (None, False)


def _run_all():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    passed = 0
    for fn in fns:
        fn()
        passed += 1
        print(f"  ✓ {fn.__name__}")
    print(f"\n{passed}/{len(fns)} passed")


if __name__ == "__main__":
    _run_all()
