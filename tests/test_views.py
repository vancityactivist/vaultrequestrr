from vaultrequestrr.cogs.requests import (
    MAX_SELECT_OPTIONS,
    ConfirmView,
    ResultSelect,
    ResultSelectView,
    SeasonSelectView,
    _PageButton,
)
from vaultrequestrr.seerr import STATUS_AVAILABLE, SearchResult, SeasonInfo, TvDetails


def _results(n):
    return [SearchResult("movie", i, f"M{i}", "2020", "x", None, None) for i in range(n)]


def test_result_view_single_page_has_no_page_buttons():
    v = ResultSelectView(None, "movie", _results(10))
    assert not [c for c in v.children if isinstance(c, _PageButton)]


def test_result_select_dedupes_duplicate_ids():
    # Two results with the same tmdb id must collapse to one option value
    # (Discord rejects duplicate select option values).
    dupes = [
        SearchResult("movie", 5, "A", "2020", "x", None, None),
        SearchResult("movie", 5, "A again", "2020", "x", None, None),
        SearchResult("movie", 6, "B", "2020", "x", None, None),
    ]
    v = ResultSelectView(None, "movie", dupes)
    sel = next(c for c in v.children if isinstance(c, ResultSelect))
    values = [o.value for o in sel.options]
    assert values == ["5", "6"]
    assert len(values) == len(set(values))


def test_result_view_paginates():
    v = ResultSelectView(None, "movie", _results(57))
    assert v._max_page == 2  # 25 + 25 + 7
    sel = next(c for c in v.children if isinstance(c, ResultSelect))
    assert len(sel.options) == MAX_SELECT_OPTIONS
    prev, nxt = [c for c in v.children if isinstance(c, _PageButton)]
    assert prev.disabled and not nxt.disabled  # first page

    v._page = v._max_page
    v._render()
    sel = next(c for c in v.children if isinstance(c, ResultSelect))
    assert len(sel.options) == 7  # remainder on the last page
    prev, nxt = [c for c in v.children if isinstance(c, _PageButton)]
    assert nxt.disabled and not prev.disabled


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
