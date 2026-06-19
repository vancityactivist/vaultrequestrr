from types import SimpleNamespace

from vaultrequestrr.cogs.requests import (
    MAX_SELECT_OPTIONS,
    ConfirmView,
    ResultSelect,
    ResultSelectView,
    SeasonSelectView,
    _my_requests_embed,
    _PageButton,
)
from vaultrequestrr.seerr import (
    REQUEST_DECLINED,
    STATUS_AVAILABLE,
    STATUS_PROCESSING,
    SearchResult,
    SeasonInfo,
    TvDetails,
)


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


def test_season_view_disabled_when_all_requested():
    details = TvDetails(3, "Show", [SeasonInfo(1, requested=True), SeasonInfo(2, requested=True)])
    v = SeasonSelectView(None, _tv(), details)
    assert v.request.disabled
    assert v.request.label == "All seasons requested"


def test_season_view_skips_already_requested_season():
    details = TvDetails(3, "Show", [SeasonInfo(1, requested=True), SeasonInfo(2)])
    v = SeasonSelectView(None, _tv(), details)

    v.selected = [1]  # already requested -> nothing new to do
    v.update_request_state()
    assert v.request.disabled

    v.selected = [2]  # not yet requested
    v.update_request_state()
    assert not v.request.disabled


def _tracked(**kw):
    base = dict(
        request_id=1, discord_id="42", media_type="movie", tmdb_id=1, title="M",
        seasons=None, request_status=None, media_status=None,
        notified_available=False, notified_declined=False,
        created_at="2026-06-18T00:00:00Z", updated_at=None,
    )
    base.update(kw)
    return SimpleNamespace(**base)


def test_my_requests_embed_renders_statuses():
    rows = [
        _tracked(request_id=1, title="Avail", media_status=STATUS_AVAILABLE),
        _tracked(request_id=2, title="Working", media_status=STATUS_PROCESSING),
        _tracked(request_id=3, title="Nope", request_status=REQUEST_DECLINED),
        _tracked(request_id=4, title="Show", media_type="tv", seasons="all"),
    ]
    desc = _my_requests_embed(rows).description
    assert "✅ Available" in desc and "Avail" in desc
    assert "⏳ Processing" in desc
    assert "❌ Declined" in desc
    assert "Show (all seasons)" in desc
    assert "🕒 Requested" in desc  # the pending TV show
