from vaultrequestrr.cogs.requests import (
    _quota_line,
    _season_emoji_text,
    _status_emoji_text,
)
from vaultrequestrr.seerr import (
    STATUS_AVAILABLE,
    STATUS_PENDING,
    STATUS_PROCESSING,
    QuotaStatus,
    SeasonInfo,
)


def test_status_emoji_text():
    assert _status_emoji_text(STATUS_AVAILABLE) == ("✅", "Available")
    assert _status_emoji_text(STATUS_PROCESSING)[1] == "Processing"
    assert _status_emoji_text(STATUS_PENDING)[1] == "Requested"
    assert _status_emoji_text(None) == (None, None)


def test_season_emoji_text():
    assert _season_emoji_text(SeasonInfo(1, available=True))[1] == "Available"
    assert _season_emoji_text(SeasonInfo(2, requested=True))[1] == "Requested"
    assert _season_emoji_text(SeasonInfo(3)) == (None, None)


def test_quota_line_unlimited():
    q = QuotaStatus(limit=0, used=4, remaining=None, restricted=False, days=7)
    assert _quota_line(q) == "Unlimited"


def test_quota_line_limited():
    q = QuotaStatus(limit=5, used=2, remaining=3, restricted=False, days=30)
    assert _quota_line(q) == "3 of 5 left (2 used in the last 30 days)"
