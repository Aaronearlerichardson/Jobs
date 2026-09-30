"""The robots.txt cache and the polite transport around it (net.http).

Offline: every case answers the request from the test (conftest.serve,
conftest.wire). No host is contacted. The RFC 9309 matching itself is
pinned by the doctests of robots._HostRules.parse.
"""

import asyncio
import contextlib
import logging
import re
import socket
import time
from types import SimpleNamespace

import aiohttp
import pytest
import requests

from conftest import answer, fake_response
from src.net import http, robots


class TestExemptHosts:
    """[policy] robots_exempt_hosts: a host on the list is fetched without
    consulting its robots.txt (SmartRecruiters' public postings API and
    PeopleAdmin's Atom feed both sit behind a blanket `Disallow: /`),
    without turning the check off for every other host."""

    @pytest.fixture
    def cache(self, monkeypatch):
        from src import config
        monkeypatch.setattr(config, "RESPECT_ROBOTS", True, raising=False)
        monkeypatch.setattr(config, "ROBOTS_EXEMPT_HOSTS",
                            ("api.smartrecruiters.com", ".peopleadmin.com"),
                            raising=False)
        c = robots.RobotsCache()
        fetched = []

        async def _blanket(origin):
            fetched.append(origin)
            return robots._HostRules(disallow_all=True)

        monkeypatch.setattr(c, "_fetch", _blanket)
        c.fetched = fetched
        return c

    async def test_exempt_host_is_allowed_without_a_robots_fetch(self, cache):
        assert await cache.allowed("https://api.smartrecruiters.com/v1/companies/x/postings")
        assert cache.fetched == []

    async def test_dotted_entry_covers_subdomains_only(self, cache):
        assert await cache.allowed("https://unc.peopleadmin.com/postings/search.atom")
        assert not await cache.allowed("https://peopleadmin.com/postings/search.atom")

    async def test_other_hosts_still_obey_their_robots(self, cache):
        assert not await cache.allowed("https://jobs.smartrecruiters.com/x")
        assert cache.fetched == ["https://jobs.smartrecruiters.com"]


def test_a_missing_protego_stops_the_program_at_start_with_the_fix():
    """Not on the first request: that was 45 logged errors and a discovery run
    that found nothing, in an environment that had not installed it yet."""
    import subprocess
    import sys
    done = subprocess.run([sys.executable, "-c", "import sys; sys.modules['protego'] = None; import src.net"],
                          capture_output=True, text=True, check=False)
    assert done.returncode != 0
    assert "pip install -r envs/requirements.txt" in done.stderr


class TestRespectRobots:
    """[policy] respect_robots is the switch, through net.http.send, the one
    door every fetcher uses: on, a page under a host's Disallow is refused
    and its Crawl-delay is waited; off, its robots.txt is not even fetched."""

    PAGE = (200, [], b"<html>jobs</html>")

    @pytest.fixture
    def waits(self, monkeypatch):
        """The Crawl-delay waits asked of the limiter, not slept."""
        waits = []
        monkeypatch.setattr(http.LIMITER, "wait",
                            answer(lambda url, delay: waits.append((url, delay))))
        return waits

    @staticmethod
    def respect(monkeypatch, on):
        from src import config
        monkeypatch.setattr(config, "RESPECT_ROBOTS", on, raising=False)

    async def test_on_a_disallowed_page_is_refused_after_one_robots_fetch(self, wire, monkeypatch):
        self.respect(monkeypatch, True)
        sent = wire((200, [], b"User-agent: *\nDisallow: /\n"))
        with pytest.raises(robots.RobotsDisallowed):
            await http.send("GET", "https://a.test/jobs")
        assert [url for _, url, _ in sent] == ["https://a.test/robots.txt"]

    async def test_on_the_hosts_crawl_delay_is_waited(self, wire, monkeypatch, waits):
        self.respect(monkeypatch, True)
        sent = wire((200, [], b"User-agent: *\nCrawl-delay: 3\n"), self.PAGE)
        assert (await http.send("GET", "https://a.test/jobs")).status_code == 200
        assert waits == [("https://a.test/jobs", 3.0)]
        assert [url for _, url, _ in sent] == ["https://a.test/robots.txt", "https://a.test/jobs"]

    async def test_off_the_same_page_is_fetched_with_no_robots_fetch_and_no_wait(
            self, wire, monkeypatch, waits):
        self.respect(monkeypatch, False)
        sent = wire(self.PAGE)
        assert (await http.send("GET", "https://a.test/jobs")).status_code == 200
        assert [url for _, url, _ in sent] == ["https://a.test/jobs"]
        assert waits == []


class TestWhatAFetchedFileMeans:
    """RFC 9309 s2.3.1 at the edge of each status range, the cache's expiry,
    and the rest of what `allowed` and `sitemaps` promise. Found by mutating
    net/robots.py (tools/mutants.py): none of it was pinned."""

    BLANKET = "User-agent: *\nDisallow: /\n"

    @pytest.fixture(autouse=True)
    def _robots_on(self, monkeypatch):
        from src import config
        monkeypatch.setattr(config, "RESPECT_ROBOTS", True, raising=False)

    @pytest.mark.parametrize("status, allowed", [
        (399, False), (400, True), (404, True), (499, True), (500, False), (599, False), (600, True),
        (601, True), (999, True)])
    async def test_each_status_means_what_the_rfc_says(self, serve, status, allowed):
        """A parsed answer obeys; a 4xx is no restriction; a 5xx is every path refused."""
        serve(fake_response(text=self.BLANKET, status=status))
        assert await robots.RobotsCache().allowed("https://a.test/jobs") is allowed

    async def test_robots_txt_is_fetched_following_redirects(self, serve):
        seen = {}
        serve(lambda url, **kw: seen.update(kw) or fake_response(text=""))
        await robots.RobotsCache()._fetch("https://example.com")
        assert seen["allow_redirects"] is True

    @pytest.mark.parametrize("elapsed, refetched", [
        (0, False), (4.999, False),       # still good
        (5.0, True), (5.001, True),       # the ttl reached, then passed
        (10_000, True)])                  # long gone
    async def test_a_file_is_fetched_again_once_its_ttl_has_passed(self, monkeypatch, elapsed, refetched):
        now, calls = [10.0], []
        monkeypatch.setattr(robots, "time", SimpleNamespace(monotonic=lambda: now[0]))

        async def fetch(origin):
            calls.append(origin)
            return robots._HostRules()

        cache = robots.RobotsCache(ttl=5)
        cache._fetch = fetch
        await cache.allowed("https://a.test/x")
        now[0] += elapsed
        await cache.allowed("https://a.test/y")
        assert (len(calls) == 2) is refetched

    async def test_a_file_that_cannot_be_parsed_fails_open(self, serve, monkeypatch):
        def refuse(text):
            raise ValueError("unparseable")
        monkeypatch.setattr(robots.Protego, "parse", staticmethod(refuse))
        serve(fake_response(text=self.BLANKET))
        rules = await robots.RobotsCache()._fetch("https://a.test")
        assert rules.protego is None and not rules.disallow_all

    async def test_an_error_while_matching_fails_open(self, monkeypatch):
        cache = robots.RobotsCache()
        monkeypatch.setattr(cache, "_fetch", answer(SimpleNamespace(allows=lambda url: 1 / 0)))
        assert await cache.allowed("https://a.test/x") is True

    async def test_sitemaps_are_the_ones_the_file_lists(self, serve):
        serve(fake_response(text="User-agent: *\nDisallow: /x\nSitemap: https://a.test/s1.xml\n"
                                 "Sitemap: https://a.test/s2.xml\n"))
        cache = robots.RobotsCache()
        assert await cache.sitemaps("https://a.test/jobs") == ["https://a.test/s1.xml", "https://a.test/s2.xml"]
        serve(fake_response(text="", status=404))
        assert await cache.sitemaps("https://b.test/jobs") == []


class TestFetchDeduplication:
    """One robots.txt fetch per host, however many requests want it at once.

    A sniff fans ~8 candidate PATHS across one host concurrently. Each one
    used to miss the still-empty cache and fetch its own copy, so a single
    company cost 8 robots.txt requests per guessed domain — and 8 full
    timeouts when the host was one that hangs instead of refusing.
    """

    @pytest.fixture(autouse=True)
    def _robots_on(self, monkeypatch):
        from src import config
        monkeypatch.setattr(config, "RESPECT_ROBOTS", True, raising=False)

    @staticmethod
    def _spy_cache(delay=0.05):
        """A RobotsCache whose network fetch is replaced by a call recorder."""
        calls, cache = [], robots.RobotsCache()

        async def _fetch(origin):
            calls.append(origin)
            await asyncio.sleep(delay)          # stand in for the round-trip
            return robots._HostRules()

        cache._fetch = _fetch
        return cache, calls

    async def test_concurrent_paths_on_one_host_fetch_once(self):
        cache, calls = self._spy_cache()
        await asyncio.gather(*(cache.allowed(f"https://example.com{p}") for p in
                               ("/careers", "/careers/open-positions", "/jobs", "/",
                                "/careers/", "/company/careers", "/join", "/x")))
        assert calls == ["https://example.com"]

    async def test_distinct_hosts_still_fetch_in_parallel(self):
        # The per-host gate must not serialize the crawl: 4 hosts x 0.2s
        # apiece completes in ~0.2s, not ~0.8s.
        cache, calls = self._spy_cache(delay=0.2)
        start = time.monotonic()
        await asyncio.gather(*(cache.allowed(f"https://h{i}.example.com/careers")
                               for i in range(4)))
        assert len(calls) == 4
        assert time.monotonic() - start < 0.6

    async def test_cached_rules_are_reused_after_the_fetch(self):
        cache, calls = self._spy_cache()
        for _ in range(3):
            await cache.allowed("https://example.com/careers")
        assert len(calls) == 1


async def test_crawl_delay_spaces_one_origin_only():
    """The limiter robots.txt's Crawl-delay feeds (RobotsCache.wait_turn):
    the next turn on an origin waits out the gap since the last one began,
    and another origin's turn goes at once."""
    limiter, at = http.HostLimiter(), {}

    async def turn(url):
        await limiter.wait(url, 0.2)
        at[url] = time.monotonic()

    t0 = time.monotonic()
    await asyncio.gather(turn("https://a.example/1"), turn("https://a.example/2"),
                         turn("https://b.example/1"))
    assert at["https://b.example/1"] - t0 < 0.1
    assert at["https://a.example/2"] - at["https://a.example/1"] >= 0.19


async def test_a_redirect_into_a_shared_host_waits_its_turn(monkeypatch):
    """A redirect hop into another origin takes that origin's turn, as a
    first request to it would: two origins redirecting into one host with
    a Crawl-delay reach it that far apart, and so do an http->https hop on
    one host and a direct request to its https origin."""
    from src import config
    monkeypatch.setattr(config, "RESPECT_ROBOTS", True, raising=False)
    monkeypatch.setattr(robots.CACHE(), "allowed", answer(True))
    paced = ("https://shared.test", "https://up.test")
    monkeypatch.setattr(robots.CACHE(), "crawl_delay",
                        answer(lambda url: 0.2 if url.startswith(paced) else None))
    at = {}

    @contextlib.asynccontextmanager
    async def request(method, url, **kw):
        url = str(url)
        if url.startswith(paced):
            at.setdefault(url[8:10], []).append(time.monotonic())
            status, headers = 200, []
        elif url.startswith("http://up.test"):
            status, headers = 302, [(b"Location", b"https://up.test/x")]
        else:
            status, headers = 302, [(b"Location", f"https://shared.test/{url[8]}".encode())]

        async def read():
            return b""
        yield SimpleNamespace(status=status, raw_headers=headers, read=read, reason="")

    monkeypatch.setattr(http, "_session", lambda: SimpleNamespace(request=request))
    got = await asyncio.gather(http.send("GET", "https://a.test/x"),
                               http.send("GET", "https://b.test/x"),
                               http.send("GET", "https://up.test/y"),
                               http.send("GET", "http://up.test/x"))
    assert [r.url for r in got] == ["https://shared.test/a", "https://shared.test/b",
                                    "https://up.test/y", "https://up.test/x"]
    for arrivals in at.values():
        assert arrivals[1] - arrivals[0] >= 0.19


class TestUnreachableHostReporting:
    """Failing OPEN is announced, but only when there is a server involved.

    Most candidates a sniff generates are speculative `careers.<name>.com`
    guesses that do not resolve. Announcing "proceeding without restrictions"
    for a host that does not exist claims a politeness decision that was never
    made, and buries the cases where a real server WAS crawled unchecked.
    """

    @staticmethod
    async def _fetch_raising(exc, serve, capsys):
        serve(exc)
        rules = await robots.RobotsCache()._fetch("https://example.com")
        return rules, capsys.readouterr().out

    async def test_nonexistent_host_is_silent(self, wire, capsys):
        """Through the transport: aiohttp's DNS error arrives as requests'
        ConnectionError, the gaierror still on its chain."""
        gai = socket.gaierror(11001, "getaddrinfo failed")
        dns = aiohttp.ClientConnectorDNSError(
            SimpleNamespace(host="example.com", port=443, ssl=True), gai)
        dns.__cause__ = gai
        wire(dns)
        rules = await robots.RobotsCache()._fetch("https://example.com")
        assert capsys.readouterr().out == ""
        assert rules.protego is None and not rules.disallow_all   # still fails open

    async def test_live_server_we_could_not_ask_is_announced(self, serve, capsys):
        _, out = await self._fetch_raising(
            requests.exceptions.SSLError("handshake"), serve, capsys)
        assert "proceeding without restrictions" in out

    async def test_timeout_to_a_resolving_host_is_announced(self, serve, capsys):
        _, out = await self._fetch_raising(
            requests.exceptions.ConnectTimeout("slow"), serve, capsys)
        assert "proceeding without restrictions" in out


class TestFetchTimeout:
    """The robots.txt fetch uses a split (connect, read) timeout.

    Discovery probes many speculative `careers.<name>.com` hosts. Those whose
    parent domain has wildcard DNS resolve to an edge that never completes a
    handshake, and under a single flat timeout each one burned the whole
    budget. Real boards connect in well under half a second (measured: median
    147 ms over 16 live boards), so the connect half can be short — but the
    read half must not be, because a host that connects promptly and answers
    slowly is a real server whose policy we still owe a wait.
    """

    @staticmethod
    async def _capture_timeout(serve):
        seen = {}
        serve(lambda url, **kw: seen.update(timeout=kw.get("timeout"))
              or requests.exceptions.ConnectTimeout("nope"))
        await robots.RobotsCache()._fetch("https://example.com")
        return seen["timeout"]

    async def test_timeout_is_a_connect_read_pair(self, serve):
        assert isinstance(await self._capture_timeout(serve), tuple)

    async def test_connect_is_shorter_than_read(self, serve):
        connect, read = await self._capture_timeout(serve)
        assert connect < read, "a short read timeout would abandon slow real servers"

    async def test_values_come_from_config(self, monkeypatch, serve):
        from src import config
        monkeypatch.setattr(config, "ROBOTS_CONNECT_TIMEOUT", 1.5, raising=False)
        monkeypatch.setattr(config, "ROBOTS_READ_TIMEOUT", 9.0, raising=False)
        assert await self._capture_timeout(serve) == (1.5, 9.0)

    async def test_a_timeout_still_fails_open(self, serve):
        """Giving up faster must not turn into giving up differently."""
        serve(requests.exceptions.ConnectTimeout("nope"))
        rules = await robots.RobotsCache()._fetch("https://example.com")
        assert rules.protego is None and not rules.disallow_all
        assert await robots.RobotsCache().allowed("https://example.com/careers") is True


class TestTransport:
    """net.http's one request: requests' preparation, redirect rules and
    reading, sent over aiohttp (the fake session: conftest.wire)."""

    async def test_redirects_are_followed_as_requests_follows_them(self, wire):
        """A 302 turns a POST into a bodiless GET on the joined URL, and a
        loop stops where requests stops it."""
        sent = wire((302, [(b"Location", b"/b?x=1")], b""),
                    (200, [(b"Content-Type", b"application/json")], b'{"ok": 1}'))
        r = await http.send("POST", "https://a.test/a", polite=False, json={"k": 1})
        assert [(m, u, kw["data"]) for m, u, kw in sent] == [
            ("POST", "https://a.test/a", b'{"k": 1}'),
            ("GET", "https://a.test/b?x=1", None)]
        assert (r.url, r.json(), len(r.history)) == ("https://a.test/b?x=1", {"ok": 1}, 1)
        sent = wire(*[(302, [(b"Location", b"/again")], b"")] * (http.MAX_REDIRECTS + 1))
        with pytest.raises(requests.TooManyRedirects):
            await http.send("GET", "https://a.test/", polite=False)
        assert len(sent) == http.MAX_REDIRECTS + 1

    async def test_a_polite_request_carries_headers_and_leaves_a_trace(
            self, wire, caplog, monkeypatch):
        """HEADERS over requests' defaults, and the session log's one http
        DEBUG line."""
        from src import config
        monkeypatch.setattr(config, "RESPECT_ROBOTS", False, raising=False)
        sent = wire((404, [], b""))
        with caplog.at_level(logging.DEBUG, logger="http"):
            r = await http.send("GET", "https://a.test/x")
        headers = sent[0][2]["headers"]
        assert (r.status_code, headers["User-Agent"], headers["Accept"]) == (
            404, http.HEADERS["User-Agent"], "*/*")
        assert re.fullmatch(r"GET https://a\.test/x -> 404 in \d+\.\d\ds",
                            caplog.records[-1].getMessage())


class TestJsProbeDisabledReporting:
    """A missing headless browser is one condition, reported once.

    The JS fallback ran several browsers in parallel, each holding its own
    enabled flag, and a web-UI process runs a pool per pass. Playwright's
    launch error embeds a ten-line ASCII banner telling you to run
    `playwright install`, so four probes printed forty lines of identical
    advice, and the sniffer's browser path had the same shape.
    """

    LAUNCH_ERR = (
        "BrowserType.launch: Executable doesn't exist at "
        r"C:\ms-playwright\chromium_headless_shell-1234\chrome-headless-shell.exe"
        "\n+------------------------------------------+"
        "\n| Looks like Playwright was just updated.  |"
        "\n|     playwright install                   |"
        "\n+------------------------------------------+"
    )

    def test_only_the_first_caller_reports(self, capsys):
        from src.discovery.resolve import probes
        assert probes._report_js_disabled("first") is True
        assert probes._report_js_disabled("second") is False
        assert probes._report_js_disabled("third") is False
        out = capsys.readouterr().out
        assert out.count("JS scan probe disabled") == 1
        assert "second" not in out and "third" not in out

    def test_missing_browser_hint_is_actionable_and_one_line(self):
        from src.discovery.resolve.probes import _js_launch_hint
        hint = _js_launch_hint(Exception(self.LAUNCH_ERR))
        assert "playwright install chromium" in hint
        assert "\n" not in hint, "the ASCII banner leaked into the log line"
        assert "+---" not in hint

    def test_unrelated_failures_keep_their_own_message(self):
        from src.discovery.resolve.probes import _js_launch_hint
        assert _js_launch_hint(
            Exception("Timeout 30000ms exceeded\nat stack line")) == "Timeout 30000ms exceeded"


class TestChromiumChannelFallback:
    """`pip install` alone should be enough to run the JS probes.

    Playwright's own browser comes from `playwright install`, a separate step
    that is routinely absent: on CI runners, on a fresh clone, and on any
    machine where the playwright PACKAGE was upgraded without re-downloading
    its browsers (the package pins a build number, so upgrading it silently
    invalidates the browser already on disk — exactly what happened here).
    Falling back to a browser the machine already has turns that from "JS
    probe disabled" into "JS probe works", with no download.
    """

    class FakePlaywright:
        """Stands in for `pw`, launching only the channels it was told exist."""

        def __init__(self, works):
            self.works = works          # set of channel names (None = bundled)
            self.tried = []
            self.chromium = self

        async def launch(self, **kw):
            channel = kw.get("channel")
            self.tried.append(channel)
            if channel not in self.works:
                raise RuntimeError(
                    "Executable doesn't exist at ...chromium_headless_shell-1234"
                    if channel is None else f"channel {channel} not found")
            return f"browser:{channel}"

    async def test_bundled_build_is_preferred(self, capsys):
        from src.discovery.resolve.probes import launch_chromium
        pw = self.FakePlaywright({None, "chrome"})
        browser, channel = await launch_chromium(pw)
        assert (browser, channel) == ("browser:None", None)
        assert pw.tried == [None], "a working bundled build must not be skipped"
        assert capsys.readouterr().out == "", "no notice when nothing fell back"

    async def test_falls_back_to_system_chrome(self, capsys):
        from src.discovery.resolve.probes import launch_chromium
        pw = self.FakePlaywright({"chrome", "msedge"})
        browser, channel = await launch_chromium(pw)
        assert (browser, channel) == ("browser:chrome", "chrome")
        assert pw.tried == [None, "chrome"]
        assert "system chrome" in capsys.readouterr().out

    async def test_falls_through_to_edge(self):
        from src.discovery.resolve.probes import launch_chromium
        pw = self.FakePlaywright({"msedge"})
        assert (await launch_chromium(pw))[1] == "msedge"
        assert pw.tried == [None, "chrome", "msedge"]

    async def test_every_channel_missing_reraises_the_bundled_error(self):
        """The bundled failure names the missing build and the install command,
        which is the actionable one — not 'msedge not found'."""
        from src.discovery.resolve.probes import launch_chromium
        pw = self.FakePlaywright(set())
        with pytest.raises(RuntimeError) as excinfo:
            await launch_chromium(pw)
        assert "chromium_headless_shell" in str(excinfo.value)

    async def test_launch_kwargs_are_passed_through(self):
        from src.discovery.resolve.probes import launch_chromium
        captured = {}

        class Recorder(self.FakePlaywright):
            async def launch(self, **kw):
                captured.update(kw)
                return await super().launch(**kw)

        await launch_chromium(Recorder({None}), headless=True)
        assert captured["headless"] is True

    async def test_the_fallback_notice_is_printed_once(self, capsys):
        from src.discovery.resolve.probes import launch_chromium
        for _ in range(4):                      # a web-UI process runs many passes
            await launch_chromium(self.FakePlaywright({"chrome"}))
        assert capsys.readouterr().out.count("system chrome") == 1

    async def test_channel_order_is_configurable(self, monkeypatch):
        from src import config
        from src.discovery.resolve.probes import launch_chromium
        monkeypatch.setattr(config, "BROWSER_CHANNELS", ["msedge", "chrome"])
        pw = self.FakePlaywright({"chrome", "msedge"})
        assert (await launch_chromium(pw))[1] == "msedge"
        assert pw.tried == ["msedge"]


class TestQuietSpeculativeProbes:
    """The "unreachable" notice is for hosts we mean to crawl.

    Discovery guesses hostnames from a company name — red.io, 410.co,
    united.ai — and fetches them to learn whether they exist. Most do not,
    and their robots.txt failure describes a politeness decision that is
    never acted on: nothing gets crawled, because the page fetch fails for
    the same reason. Those failures were always happening; robots.txt was
    just the first code to report them, which turned a silent miss into a
    line of log per guess and buried the notices that matter.
    """

    async def test_speculative_failures_are_silent(self, serve, capsys):
        serve(requests.exceptions.SSLError("handshake"))
        with robots.quiet():
            await robots.CACHE()._fetch("https://red.io")
        assert capsys.readouterr().out == ""

    async def test_real_targets_still_report(self, serve, capsys):
        serve(requests.exceptions.SSLError("handshake"))
        await robots.RobotsCache()._fetch("https://jobs.example.com")
        assert "proceeding without restrictions" in capsys.readouterr().out

    def test_quiet_does_not_leak_past_its_block(self):
        with robots.quiet():
            pass
        assert robots.CACHE().quiet == 0

    def test_quiet_restores_on_exception(self):
        with pytest.raises(ValueError):
            with robots.quiet():
                raise ValueError("boom")
        assert robots.CACHE().quiet == 0, "a raising probe must not mute the crawl"

    def test_quiet_nests(self):
        with robots.quiet():
            with robots.quiet():
                assert robots.CACHE().quiet == 2
            assert robots.CACHE().quiet == 1, "the inner exit silenced the outer block"
        assert robots.CACHE().quiet == 0

    async def test_quiet_never_changes_what_is_allowed(self, serve):
        """Silence is a logging decision, not a politeness one."""
        serve(requests.exceptions.SSLError("handshake"))
        with robots.quiet():
            rules = await robots.CACHE()._fetch("https://red.io")
        assert rules.protego is None and not rules.disallow_all   # still fails open
