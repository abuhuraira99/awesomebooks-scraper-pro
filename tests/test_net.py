"""Rate limiter and HTTP client behaviour under throttling and failures."""

import random
import threading

import pytest

from awesomebooks_pro.net import AdaptiveRateLimiter, BlockedError, HttpClient, IdentityFactory, NotFoundError
from tests.conftest import FakeResponse


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def test_limiter_spaces_requests_globally():
    clock = Clock()
    lim = AdaptiveRateLimiter(1.0, jitter=0.0, clock=clock)
    waits = [lim.reserve() for _ in range(4)]
    assert waits == [0.0, 1.0, 2.0, 3.0]  # four sessions asking at once still get 1 req/s


def test_limiter_warning_slows_down_and_cools_everyone():
    clock = Clock()
    lim = AdaptiveRateLimiter(1.0, jitter=0.0, warning_cooldown=30, clock=clock, rng=random.Random(1))
    cooldown = lim.record_warning()
    snap = lim.snapshot()
    assert snap.current_interval == 2.0
    assert 30 <= cooldown <= 37.5
    assert lim.reserve() == pytest.approx(cooldown)


def test_limiter_honours_retry_after_and_trips_breaker():
    clock = Clock()
    lim = AdaptiveRateLimiter(
        1.0, jitter=0.0, warning_cooldown=1, breaker_threshold=3, breaker_cooldown=200, clock=clock, rng=random.Random(0)
    )
    assert lim.record_warning(retry_after=120) >= 120
    lim.record_warning()
    assert lim.record_warning() >= 200
    assert lim.snapshot().breaker_trips == 1


def test_limiter_recovers_after_clean_streak():
    lim = AdaptiveRateLimiter(1.0, jitter=0.0, recover_after=5, clock=Clock())
    lim.record_warning()
    assert lim.snapshot().current_interval == 2.0
    for _ in range(5):
        lim.record_success()
    assert lim.snapshot().current_interval == pytest.approx(1.6)
    for _ in range(50):
        lim.record_success()
    assert lim.snapshot().current_interval == 1.0  # never faster than the chosen pace


class ScriptedTransport:
    def __init__(self, script):
        self.script = script
        self.calls = 0

    def get(self, url, *, params, headers, timeout):
        self.calls += 1
        step = self.script.pop(0)
        if isinstance(step, Exception):
            raise step
        return step

    def post(self, url, *, data, headers, timeout):
        self.posted = (url, data, headers)
        return self.get(url, params=None, headers=headers, timeout=timeout)

    def close(self):
        pass


def make_client(script, **kw):
    transport = ScriptedTransport(script)
    identities = []

    def factory(identity):
        identities.append(identity)
        return transport

    lim = AdaptiveRateLimiter(0.0, jitter=0.0, warning_cooldown=0.0, breaker_cooldown=0.0)
    events = []
    client = HttpClient(
        lim,
        IdentityFactory(rng=random.Random(3)),
        transport_factory=factory,
        on_event=lambda lvl, m: events.append((lvl, m)),
        backoff_scale=0.0,
        **kw,
    )
    return client, transport, identities, events


def test_client_recovers_from_throttling_with_new_identity():
    client, transport, identities, events = make_client([FakeResponse(429, "slow down"), FakeResponse(200, '{"ok": true}')])
    assert client.get_json("https://x/a.json") == {"ok": True}
    assert len(identities) == 2  # identity retired after the 429
    assert client.stats.warnings == 1 and client.stats.successes == 1
    assert any(level == "warn" for level, _ in events)


def test_client_treats_html_on_json_endpoint_as_block():
    client, *_ = make_client([FakeResponse(200, "<html>Just a moment...</html>"), FakeResponse(200, "[]")])
    assert client.get_json("https://x/a.json") == []
    assert client.stats.warnings == 1


def test_client_404_is_definitive():
    client, transport, *_ = make_client([FakeResponse(404, "")])
    with pytest.raises(NotFoundError):
        client.get_text("https://x/missing")
    assert transport.calls == 1


def test_client_retries_network_errors():
    client, transport, identities, _ = make_client([ConnectionError("reset"), ConnectionError("reset"), FakeResponse(200, "ok")])
    assert client.get_text("https://x/") == "ok"
    assert client.stats.network_errors == 2
    assert len(identities) == 2


def test_client_gives_up_when_blocked_persistently():
    client, *_ = make_client([FakeResponse(430, "blocked")] * 3, max_attempts=3)
    with pytest.raises(BlockedError):
        client.get_text("https://x/")


def test_identity_budget_rotates_identities():
    script = [FakeResponse(200, "ok") for _ in range(12)]
    client, _, identities, _ = make_client(script)
    client.identities._budget_range = (3, 3)
    client.rotate_identity()
    for _ in range(12):
        client.get_text("https://x/")
    assert len(identities) >= 4


def test_cancel_stops_waiting_immediately():
    lim = AdaptiveRateLimiter(30.0, jitter=0.0)
    lim.reserve()  # next slot is 30s away
    cancel = threading.Event()
    cancel.set()
    assert lim.acquire(cancel) is False


def test_proxies_round_robin():
    factory = IdentityFactory(["http://p1:1", "http://p2:2"], rng=random.Random(0))
    proxies = {factory.create().proxy for _ in range(4)}
    assert proxies == {"http://p1:1", "http://p2:2"}


class DNSError(Exception):
    pass


def test_internet_outage_waits_without_spending_attempts():
    outage = [DNSError("curl: (6) Could not resolve host: awesomebooks.com")] * 10
    client, transport, _, events = make_client(
        [*outage, FakeResponse(200, "ok")], max_attempts=3, connectivity_check=lambda: False
    )
    assert client.get_text("https://x/") == "ok"  # 10 offline failures > 3 attempts, still succeeds
    levels = [lvl for lvl, _ in events]
    assert levels.count("warn") == 1 and "success" in levels  # one "lost" and one "restored" event
    assert client.stats.offline_events == 1 and client.stats.offline_since is None


def test_other_network_errors_still_give_up():
    from awesomebooks_pro.net import FetchError

    client, *_ = make_client([ConnectionError("connection reset by peer")] * 3, max_attempts=3)
    with pytest.raises(FetchError):
        client.get_text("https://x/")


def test_requests_fallback_only_advertises_decodable_encodings():
    from awesomebooks_pro.net.client import _RequestsTransport

    identity = IdentityFactory(rng=random.Random(0)).create()
    transport = _RequestsTransport(identity)
    assert "br" not in transport._session.headers["Accept-Encoding"]  # undecodable bodies looked like block pages
    assert transport._session.headers["User-Agent"] == identity.profile.user_agent
    transport.close()


def test_site_refusing_connections_is_not_mistaken_for_an_outage():
    from awesomebooks_pro.net import FetchError

    refused = [ConnectionError("curl: (7) Failed to connect to awesomebooks.com port 443")] * 3
    client, _, _, events = make_client(refused, max_attempts=3, connectivity_check=lambda: True)
    with pytest.raises(FetchError):  # the internet works, so this is the site: attempts are spent, no endless wait
        client.get_text("https://x/")
    assert client.stats.offline_events == 0


def test_normal_page_mentioning_captcha_is_not_a_block():
    page = "<html><head><script src='https://www.google.com/recaptcha/api.js'></script></head><body>ok</body></html>"
    client, *_ = make_client([FakeResponse(200, page)])
    assert client.get_text("https://x/") == page and client.stats.warnings == 0


def test_connectivity_is_rechecked_so_a_refusing_site_is_not_waited_on_forever():
    from awesomebooks_pro.net import FetchError

    state = {"online": False}
    dns = [ConnectionError("curl: (6) Could not resolve host: awesomebooks.com")] * 2
    refused = [ConnectionError("curl: (7) Failed to connect to awesomebooks.com")] * 3
    client, *_ = make_client([*dns, *refused], max_attempts=3, connectivity_check=lambda: state["online"])

    original = client._offline_confirmed

    def flip():  # the internet comes back after the DNS failures
        if client.stats.network_errors > 2:
            state["online"] = True
            client._probe_at = -1e9
        return original()

    client._offline_confirmed = flip
    with pytest.raises(FetchError):
        client.get_text("https://x/")


def test_cloudflare_interstitial_triggers_a_cooldown():
    page = (
        "<!DOCTYPE html><html><head><title>Just a moment...</title></head><body>"
        "<script>window._cf_chl_opt={cvId: '3'}</script></body></html>"
    )
    client, *_ = make_client([FakeResponse(200, page), FakeResponse(200, "<html>ok</html>")])
    assert client.get_text("https://x/") == "<html>ok</html>"
    assert client.stats.warnings == 1


def test_cloudflare_passive_script_on_normal_pages_is_not_a_block():
    # Every real AwesomeBooks page loads this script; it must never be mistaken for a challenge.
    page = (
        "<html><head><title>Fiction | Awesome Books</title></head><body>books"
        "<script>a.src='/cdn-cgi/challenge-platform/scripts/jsd/main.js';</script></body></html>"
    )
    client, *_ = make_client([FakeResponse(200, page)])
    assert client.get_text("https://x/") == page and client.stats.warnings == 0


def test_any_403_is_treated_as_a_firewall_verdict():
    client, _, identities, _ = make_client([FakeResponse(403, "Forbidden"), FakeResponse(200, "<html>ok</html>")])
    assert client.get_text("https://x/") == "<html>ok</html>"
    assert client.stats.warnings == 1 and len(identities) == 2


def test_post_json_sends_a_json_body_and_bumps_the_identity_serial_on_rotation():
    client, transport, *_ = make_client([FakeResponse(429, "slow"), FakeResponse(200, '{"results": []}')])
    serial_before = client.identity_serial
    assert client.post_json("https://x/psearch", {"requests": [{"indexName": "books"}]}, headers={"X-CSRF-TOKEN": "t"}) == {
        "results": []
    }
    url, body, headers = transport.posted
    assert body == '{"requests":[{"indexName":"books"}]}'
    assert headers["Content-Type"] == "application/json" and headers["X-CSRF-TOKEN"] == "t"
    assert client.identity_serial == serial_before + 2  # first identity, then a fresh one after the 429
