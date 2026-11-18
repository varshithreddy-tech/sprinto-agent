"""URL classification into rubric question types (pure)."""
from scraper.discover import classify


def test_whole_word_matching():
    assert "compliance" not in classify("https://www.bewakoof.com/socks-for-men")
    assert "compliance" in classify("https://acme.io/soc-2")
    assert "compliance" in classify("https://acme.io/security")
    assert "compliance" in classify("https://trust.acme.io/")
    assert "buyer" in classify("https://acme.io/customers/acme-case-study")
    assert "timing" in classify("https://acme.io/careers")
    assert classify("https://acme.io/securities-law") == []
