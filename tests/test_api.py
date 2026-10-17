from datetime import datetime, timedelta, timezone

import httpx
import pytest

from lolesports_ical.api import ApiConfig, LolEsportsAPIClient, api_fetch_leagues
from lolesports_ical.util import DiskCache, Fetcher, RateLimiter, RetryConfig


def event(match_id="123", slug="new_league", days=0):
    return {
        "type": "match",
        "state": "completed",
        "startTime": (datetime.now(timezone.utc) + timedelta(days=days)).isoformat(),
        "league": {"slug": slug, "name": "New League"},
        "match": {
            "id": match_id,
            "teams": [
                {"name": "A", "result": {"gameWins": 2, "outcome": "win"}},
                {"name": "B", "result": {"gameWins": 1}},
            ],
        },
    }


@pytest.fixture
def make_client(tmp_path):
    fetcher = Fetcher(
        cache=DiskCache(tmp_path),
        rate_limiter=RateLimiter(0),
        retry=RetryConfig(max_attempts=1),
    )
    clients = []

    def make(handler, fallback=None, config=None):
        fetcher.client.close()
        fetcher.client = httpx.Client(transport=httpx.MockTransport(handler))
        client = LolEsportsAPIClient(config or ApiConfig(), fetcher, fallback or (lambda **kw: []))
        clients.append(client)
        return client

    yield make
    fetcher.close()


def test_paginates_both_directions_and_discovers_new_leagues(make_client):
    calls = []
    pages = {
        None: {"events": [event()], "pages": {"newer": "next", "older": "past"}},
        "next": {"events": [event("future", days=31)], "pages": {"newer": "not-needed"}},
        "past": {"events": [event("finished", days=-8)], "pages": {"older": "not-needed"}},
    }

    def handler(request):
        token = request.url.params.get("pageToken")
        calls.append(token)
        return httpx.Response(200, json={"data": {"schedule": pages[token]}})

    client = make_client(handler)
    matches = client.fetch_matches([])
    assert {m.match_id for m in matches} == {"123", "future", "finished"}
    assert all(m.league_slug == "new_league" for m in matches)
    assert calls == [None, "next", "past"]
    assert matches[0].winner == "A"


def test_explicit_league_filter(make_client):
    client = make_client(
        lambda request: httpx.Response(
            200,
            json={
                "data": {
                    "schedule": {
                        "events": [event(slug="lec"), event(slug="new_league")],
                        "pages": {},
                    }
                }
            },
        )
    )
    assert [m.league_slug for m in client.fetch_matches(["lec"])] == ["lec"]


@pytest.mark.parametrize(
    "status,payload",
    [
        (403, {}),
        (200, {"data": {"schedule": {"events": [], "pages": {}}}}),
        (200, {"data": {}}),
    ],
)
def test_errors_and_empty_schedule_use_injected_fallback(make_client, status, payload):
    calls = []
    client = make_client(
        lambda request: httpx.Response(status, json=payload),
        fallback=lambda **kwargs: calls.append(kwargs) or ["fallback"],
    )
    assert client.fetch_matches(["new_league"]) == ["fallback"]
    assert calls[0]["league_slugs"] == ["new_league"]


def test_repeated_pagination_token_does_not_loop(make_client):
    client = make_client(
        lambda request: httpx.Response(
            200,
            json={
                "data": {
                    "schedule": {
                        "events": [event()],
                        "pages": {"newer": "same"},
                    }
                }
            },
        ),
        fallback=lambda **kwargs: ["fallback"],
    )
    assert client.fetch_matches([]) == ["fallback"]


def test_http_cache_is_reused(make_client):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={"data": {"schedule": {"events": [event()], "pages": {}}}})

    client = make_client(handler)
    assert client.fetch_matches([]) == client.fetch_matches([])
    assert len(calls) == 1


def test_league_directory_validates_slugs_and_accepts_new_entries(make_client):
    client = make_client(
        lambda request: httpx.Response(
            200,
            json={
                "data": {
                    "leagues": [
                        {"slug": "new-league", "name": " New League ", "region": "EMEA"},
                        {"slug": "../bad", "name": "Bad"},
                    ]
                }
            },
        )
    )
    assert api_fetch_leagues(client.fetcher) == [
        {"slug": "new-league", "name": "New League", "region": "EMEA"},
    ]


@pytest.mark.parametrize(
    "image,expected",
    [
        (
            "http://static.lolesports.com/leagues/lec.png",
            "https://static.lolesports.com/leagues/lec.png",
        ),
        (
            "https://static.lolesports.com/leagues/lec.png",
            "https://static.lolesports.com/leagues/lec.png",
        ),
        (None, None),
        ("javascript:alert(1)", None),
        ("data:image/svg+xml,test", None),
    ],
)
def test_league_logos_use_https_or_are_omitted(make_client, image, expected):
    client = make_client(
        lambda request: httpx.Response(
            200,
            json={
                "data": {
                    "leagues": [
                        {"slug": "lec", "name": "LEC", "image": image},
                    ]
                }
            },
        )
    )
    league = api_fetch_leagues(client.fetcher)[0]
    assert league.get("image") == expected


def test_upstream_hidden_status_is_preserved(make_client):
    client = make_client(
        lambda request: httpx.Response(
            200,
            json={
                "data": {
                    "leagues": [
                        {
                            "slug": "retired",
                            "name": "Retired",
                            "displayPriority": {"status": "hidden"},
                        },
                    ]
                }
            },
        )
    )
    assert api_fetch_leagues(client.fetcher)[0]["hidden"] is True


def test_null_scores_do_not_discard_the_schedule(make_client):
    item = event()
    item["match"]["teams"][0]["result"]["gameWins"] = None
    client = make_client(
        lambda request: httpx.Response(
            200,
            json={
                "data": {
                    "schedule": {
                        "events": [item],
                        "pages": {},
                    }
                }
            },
        )
    )
    matches = client.fetch_matches([])
    assert len(matches) == 1
    assert matches[0].team1_score is None
    assert matches[0].team2_score == 1


def test_empty_league_directory_is_an_error(make_client):
    client = make_client(lambda request: httpx.Response(200, json={"data": {"leagues": []}}))
    with pytest.raises(ValueError, match="Empty league directory"):
        api_fetch_leagues(client.fetcher)
