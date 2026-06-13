from vaultrequestrr.cogs.requests import ConfirmView, SeasonSelectView
from vaultrequestrr.seerr import STATUS_AVAILABLE, SearchResult, SeasonInfo, TvDetails


def _movie(status=None):
    return SearchResult("movie", 1, "M", "2020", "x", None, status)


def test_confirm_view_disables_when_available():
    v = ConfirmView(None, "movie", _movie(STATUS_AVAILABLE), None)
    assert v.request.disabled
    assert v.request.label == "Already available"


def test_confirm_view_enabled_when_not_available():
    v = ConfirmView(None, "movie", _movie(None), None)
    assert not v.request.disabled


def _tv():
    return SearchResult("tv", 3, "Show", "2020", "x", None, None)


def test_season_view_disabled_when_all_available():
    details = TvDetails(3, "Show", [SeasonInfo(1, available=True), SeasonInfo(2, available=True)])
    v = SeasonSelectView(None, _tv(), details)
    assert v.request.disabled
    assert v.request.label == "All seasons available"


def test_season_view_enabled_with_some_missing():
    details = TvDetails(3, "Show", [SeasonInfo(1, available=True), SeasonInfo(2)])
    v = SeasonSelectView(None, _tv(), details)
    assert not v.request.disabled  # default "all" includes the missing S2


def test_season_view_disabled_when_selecting_only_available():
    details = TvDetails(3, "Show", [SeasonInfo(1, available=True), SeasonInfo(2)])
    v = SeasonSelectView(None, _tv(), details)

    v.selected = [1]  # only the already-available season
    v.update_request_state()
    assert v.request.disabled

    v.selected = [2]  # a missing season
    v.update_request_state()
    assert not v.request.disabled
