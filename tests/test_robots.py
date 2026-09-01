"""RFC 9309 path matching.

Offline: every case parses a robots.txt body from a string. No host is
contacted, so these pin the MATCHER, not any site's current policy.

Why this file exists: `urllib.robotparser` matches with
`path.startswith(rule)` and takes the first rule in file order. That is
wrong in both directions — it over-blocks hosts whose carve-outs use `*`
(Hacker News publishes `Allow: /*.json$` for exactly the API this crawler
reads, and stdlib turned the whole source off), and it under-blocks
wildcard `Disallow:` patterns, which is the direction that would have us
fetching paths a host asked us to leave alone.
"""

import time

import pytest

from scrapers.robots import _match_group, _pattern_to_re, parse_groups

UA = "Mozilla/5.0 (Windows NT 10.0) Chrome/124.0.0.0 Safari/537.36"


def allows(body, path, user_agent=UA):
    group = _match_group(parse_groups(body), user_agent)
    return True if group is None else group.allows(path)


class TestWildcards:
    """§2.2.3 — `*` and `$` MUST be supported."""

    # Verbatim from https://hacker-news.firebaseio.com/robots.txt: a
    # deliberate carve-out letting automated clients read the JSON API while
    # everything else is off-limits.
    HN = ("User-agent: *\n"
          "Allow: /*.json$\n"
          "Allow: /*.json?*$\n"
          "Disallow: /")

    def test_wildcard_allow_reopens_the_json_api(self):
        assert allows(self.HN, "/v0/user/whoishiring.json")
        assert allows(self.HN, "/v0/item/38912345.json")

    def test_non_json_paths_stay_blocked(self):
        assert not allows(self.HN, "/v0/whatever")
        assert not allows(self.HN, "/")

    def test_dollar_anchors_the_end(self):
        body = "User-agent: *\nDisallow: /x$"
        assert not allows(body, "/x")          # exact match: blocked
        assert allows(body, "/x/y")            # anchored, so not a match

    def test_wildcard_disallow_actually_bites(self):
        # The under-blocking direction: stdlib never matched this at all.
        assert not allows("User-agent: *\nDisallow: /*/private", "/x/private")

    def test_query_string_is_matchable(self):
        assert not allows("User-agent: *\nDisallow: /*?feed=", "/?feed=job_feed")


class TestSpecificity:
    """§2.2.2 — the longest match wins, and Allow breaks ties."""

    def test_longest_match_beats_file_order(self):
        # stdlib returns the FIRST match, so it read this as a blanket block.
        assert allows("User-agent: *\nDisallow: /\nAllow: /api/", "/api/x")

    def test_allow_wins_an_exact_tie(self):
        assert allows("User-agent: *\nDisallow: /a\nAllow: /a", "/a")

    def test_more_specific_disallow_beats_broader_allow(self):
        body = "User-agent: *\nAllow: /api/\nDisallow: /api/internal/"
        assert allows(body, "/api/public")
        assert not allows(body, "/api/internal/x")


class TestExemptHosts:
    """[policy] robots_exempt_hosts: a host on the list is fetched without
    consulting its robots.txt (SmartRecruiters' public postings API and
    PeopleAdmin's Atom feed both sit behind a blanket `Disallow: /`),
    without turning the check off for every other host."""

    @pytest.fixture
    def cache(self, monkeypatch):
        import config
        from scrapers.robots import RobotsCache, _HostRules
        monkeypatch.setattr(config, "RESPECT_ROBOTS", True, raising=False)
        monkeypatch.setattr(config, "ROBOTS_EXEMPT_HOSTS",
                            ("api.smartrecruiters.com", ".peopleadmin.com"),
                            raising=False)
        c = RobotsCache()
        fetched = []

        def _blanket(origin):
            fetched.append(origin)
            return _HostRules(disallow_all=True)

        monkeypatch.setattr(c, "_fetch", _blanket)
        c.fetched = fetched
        return c

    def test_exempt_host_is_allowed_without_a_robots_fetch(self, cache):
        assert cache.allowed("https://api.smartrecruiters.com/v1/companies/x/postings")
        assert cache.fetched == []

    def test_dotted_entry_covers_subdomains_only(self, cache):
        assert cache.allowed("https://unc.peopleadmin.com/postings/search.atom")
        assert not cache.allowed("https://peopleadmin.com/postings/search.atom")

    def test_other_hosts_still_obey_their_robots(self, cache):
        assert not cache.allowed("https://jobs.smartrecruiters.com/x")
        assert cache.fetched == ["https://jobs.smartrecruiters.com"]


class TestGroups:
    def test_blanket_disallow(self):
        assert not allows("User-agent: *\nDisallow: /", "/anything")

    def test_empty_disallow_means_allow_all(self):
        assert allows("User-agent: *\nDisallow:", "/anything")

    def test_rules_for_other_bots_do_not_apply_to_us(self):
        # Several job hosts allow Googlebot/Twitterbot and no one else.
        body = ("User-agent: Googlebot\nAllow: /\n\n"
                "User-agent: *\nDisallow: /")
        assert not allows(body, "/postings")
        assert allows(body, "/postings", user_agent="Googlebot/2.1")

    def test_consecutive_user_agents_share_one_group(self):
        body = "User-agent: a\nUser-agent: b\nDisallow: /x"
        assert not allows(body, "/x", user_agent="bot-a-1.0")
        assert not allows(body, "/x", user_agent="bot-b-1.0")

    def test_no_rules_at_all_is_unrestricted(self):
        assert allows("# just a comment", "/x")
        assert allows("", "/x")

    def test_comments_and_blank_lines_are_ignored(self):
        assert not allows("# hi\n\nUser-agent: *   # us\nDisallow: /x  # no\n",
                          "/x")


class TestPatternCompiler:
    @pytest.mark.parametrize("pattern,path,expected", [
        ("/a.b", "/a.b", True),
        ("/a.b", "/axb", False),      # `.` is literal, not a regex wildcard
        ("/a*b", "/axxxb", True),
        ("/a*b", "/ab", True),
        ("/p", "/prefix/deep", True),  # unanchored patterns are prefixes
    ])
    def test_literal_characters_are_escaped(self, pattern, path, expected):
        assert bool(_pattern_to_re(pattern).match(path)) is expected


class TestFetchDeduplication:
    """One robots.txt fetch per host, however many threads want it at once.

    A sniff fans ~8 candidate PATHS across one host concurrently. Each thread
    used to miss the still-empty cache and fetch its own copy, so a single
    company cost 8 robots.txt requests per guessed domain — and 8 full
    timeouts when the host was one that hangs instead of refusing.
    """

    @staticmethod
    def _spy_cache(delay=0.05):
        """A RobotsCache whose network fetch is replaced by a call recorder."""
        import threading

        from scrapers import robots

        calls, lock = [], threading.Lock()

        def _fetch(self, origin):
            with lock:
                calls.append(origin)
            time.sleep(delay)              # stand in for the round-trip
            return robots._HostRules()

        cache = robots.RobotsCache()
        cache._fetch = _fetch.__get__(cache, robots.RobotsCache)
        return cache, calls

    @staticmethod
    def _hammer(cache, urls):
        import threading

        threads = [threading.Thread(target=cache.allowed, args=(u,)) for u in urls]
        start = time.monotonic()
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        return time.monotonic() - start

    def test_concurrent_paths_on_one_host_fetch_once(self):
        cache, calls = self._spy_cache()
        self._hammer(cache, [f"https://example.com{p}" for p in
                             ("/careers", "/careers/open-positions", "/jobs", "/",
                              "/careers/", "/company/careers", "/join", "/x")])
        assert calls == ["https://example.com"]

    def test_distinct_hosts_still_fetch_in_parallel(self):
        # The per-host gate must not serialize the crawl: 4 hosts x 0.2s
        # apiece completes in ~0.2s, not ~0.8s.
        cache, calls = self._spy_cache(delay=0.2)
        elapsed = self._hammer(cache, [f"https://h{i}.example.com/careers"
                                       for i in range(4)])
        assert len(calls) == 4
        assert elapsed < 0.6

    def test_cached_rules_are_reused_after_the_fetch(self):
        cache, calls = self._spy_cache()
        for _ in range(3):
            cache.allowed("https://example.com/careers")
        assert len(calls) == 1


class TestUnreachableHostReporting:
    """Failing OPEN is announced, but only when there is a server involved.

    Most candidates a sniff generates are speculative `careers.<name>.com`
    guesses that do not resolve. Announcing "proceeding without restrictions"
    for a host that does not exist claims a politeness decision that was never
    made, and buries the cases where a real server WAS crawled unchecked.
    """

    @staticmethod
    def _fetch_raising(exc, capsys):
        import requests

        from scrapers import robots

        cache = robots.RobotsCache()
        original = requests.get
        requests.get = lambda *a, **k: (_ for _ in ()).throw(exc)
        try:
            rules = cache._fetch("https://example.com")
        finally:
            requests.get = original
        return rules, capsys.readouterr().out

    def test_nonexistent_host_is_silent(self, capsys):
        import socket

        import requests

        exc = requests.exceptions.ConnectionError("nope")
        exc.__cause__ = socket.gaierror(11001, "getaddrinfo failed")
        rules, out = self._fetch_raising(exc, capsys)
        assert out == ""
        assert rules.group is None and not rules.disallow_all   # still fails open

    def test_live_server_we_could_not_ask_is_announced(self, capsys):
        import requests

        _, out = self._fetch_raising(requests.exceptions.SSLError("handshake"), capsys)
        assert "proceeding without restrictions" in out

    def test_timeout_to_a_resolving_host_is_announced(self, capsys):
        import requests

        _, out = self._fetch_raising(requests.exceptions.ConnectTimeout("slow"), capsys)
        assert "proceeding without restrictions" in out


class TestDnsFailureDetection:
    def test_walks_the_wrapped_cause_chain(self):
        import socket

        from scrapers.robots import _is_dns_failure

        inner = socket.gaierror(11001, "getaddrinfo failed")
        middle = OSError("Max retries exceeded")
        middle.__cause__ = inner
        outer = Exception("HTTPSConnectionPool(...)")
        outer.__cause__ = middle
        assert _is_dns_failure(outer)

    def test_unrelated_errors_are_not_dns_failures(self):
        from scrapers.robots import _is_dns_failure

        assert not _is_dns_failure(TimeoutError("timed out"))
        assert not _is_dns_failure(None)

    def test_survives_a_cyclic_exception_chain(self):
        from scrapers.robots import _is_dns_failure

        a, b = Exception("a"), Exception("b")
        a.__cause__, b.__cause__ = b, a
        assert not _is_dns_failure(a)      # terminates rather than spinning


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
    def _capture_timeout(monkeypatch):
        import requests

        from scrapers import robots

        seen = {}

        def fake_get(url, **kw):
            seen["timeout"] = kw.get("timeout")
            raise requests.exceptions.ConnectTimeout("nope")

        monkeypatch.setattr(requests, "get", fake_get)
        robots.RobotsCache()._fetch("https://example.com")
        return seen["timeout"]

    def test_timeout_is_a_connect_read_pair(self, monkeypatch):
        assert isinstance(self._capture_timeout(monkeypatch), tuple)

    def test_connect_is_shorter_than_read(self, monkeypatch):
        connect, read = self._capture_timeout(monkeypatch)
        assert connect < read, "a short read timeout would abandon slow real servers"

    def test_values_come_from_config(self, monkeypatch):
        import config
        monkeypatch.setattr(config, "ROBOTS_CONNECT_TIMEOUT", 1.5, raising=False)
        monkeypatch.setattr(config, "ROBOTS_READ_TIMEOUT", 9.0, raising=False)
        assert self._capture_timeout(monkeypatch) == (1.5, 9.0)

    def test_a_timeout_still_fails_open(self, monkeypatch):
        """Giving up faster must not turn into giving up differently."""
        import requests

        from scrapers import robots

        monkeypatch.setattr(requests, "get", lambda url, **kw: (_ for _ in ()).throw(
            requests.exceptions.ConnectTimeout("nope")))
        rules = robots.RobotsCache()._fetch("https://example.com")
        assert rules.group is None and not rules.disallow_all
        assert robots.RobotsCache().allowed("https://example.com/careers") is True


class TestJsProbeDisabledReporting:
    """A missing headless browser is one condition, reported once.

    The JS fallback runs several WorkdayJsProbe instances in parallel, each
    holding its own enabled flag. Playwright's launch error embeds a ten-line
    ASCII banner telling you to run `playwright install`, so four probes
    printed forty lines of identical advice — and the sniffer's browser path
    had the same shape.
    """

    LAUNCH_ERR = (
        "BrowserType.launch: Executable doesn't exist at "
        r"C:\ms-playwright\chromium_headless_shell-1234\chrome-headless-shell.exe"
        "\n+------------------------------------------+"
        "\n| Looks like Playwright was just updated.  |"
        "\n|     playwright install                   |"
        "\n+------------------------------------------+"
    )

    @pytest.fixture(autouse=True)
    def _rearm(self):
        from discovery import probes
        probes._clear_js_disabled()
        yield
        probes._clear_js_disabled()

    def test_only_the_first_caller_reports(self, capsys):
        from discovery import probes
        assert probes._report_js_disabled("first") is True
        assert probes._report_js_disabled("second") is False
        assert probes._report_js_disabled("third") is False
        out = capsys.readouterr().out
        assert out.count("JS workday probe disabled") == 1
        assert "second" not in out and "third" not in out

    def test_concurrent_callers_report_once(self, capsys):
        import threading

        from discovery import probes
        results, lock = [], threading.Lock()

        def go():
            r = probes._report_js_disabled("launch failed")
            with lock:
                results.append(r)

        threads = [threading.Thread(target=go) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert results.count(True) == 1
        assert capsys.readouterr().out.count("JS workday probe disabled") == 1

    def test_missing_browser_hint_is_actionable_and_one_line(self):
        from discovery.probes import _js_launch_hint
        hint = _js_launch_hint(Exception(self.LAUNCH_ERR))
        assert "playwright install chromium" in hint
        assert "\n" not in hint, "the ASCII banner leaked into the log line"
        assert "+---" not in hint

    def test_unrelated_failures_keep_their_own_message(self):
        from discovery.probes import _js_launch_hint
        assert _js_launch_hint(
            Exception("Timeout 30000ms exceeded\nat stack line")) == "Timeout 30000ms exceeded"

    def test_a_successful_launch_rearms_the_notice(self, capsys):
        """Otherwise a web-UI process that recovers, then breaks again, goes
        quiet about the second failure for the rest of its life."""
        from discovery import probes
        assert probes._report_js_disabled("failure one") is True
        probes._clear_js_disabled()               # what a successful launch does
        assert probes._report_js_disabled("failure two") is True
        assert capsys.readouterr().out.count("JS workday probe disabled") == 2


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

        def launch(self, **kw):
            channel = kw.get("channel")
            self.tried.append(channel)
            if channel not in self.works:
                raise RuntimeError(
                    "Executable doesn't exist at ...chromium_headless_shell-1234"
                    if channel is None else f"channel {channel} not found")
            return f"browser:{channel}"

    @pytest.fixture(autouse=True)
    def _quiet(self):
        from discovery import probes
        probes._JS_NOTICES.clear()
        yield
        probes._JS_NOTICES.clear()

    def test_bundled_build_is_preferred(self, capsys):
        from discovery.probes import launch_chromium
        pw = self.FakePlaywright({None, "chrome"})
        browser, channel = launch_chromium(pw)
        assert (browser, channel) == ("browser:None", None)
        assert pw.tried == [None], "a working bundled build must not be skipped"
        assert capsys.readouterr().out == "", "no notice when nothing fell back"

    def test_falls_back_to_system_chrome(self, capsys):
        from discovery.probes import launch_chromium
        pw = self.FakePlaywright({"chrome", "msedge"})
        browser, channel = launch_chromium(pw)
        assert (browser, channel) == ("browser:chrome", "chrome")
        assert pw.tried == [None, "chrome"]
        assert "system chrome" in capsys.readouterr().out

    def test_falls_through_to_edge(self):
        from discovery.probes import launch_chromium
        pw = self.FakePlaywright({"msedge"})
        assert launch_chromium(pw)[1] == "msedge"
        assert pw.tried == [None, "chrome", "msedge"]

    def test_every_channel_missing_reraises_the_bundled_error(self):
        """The bundled failure names the missing build and the install command,
        which is the actionable one — not 'msedge not found'."""
        from discovery.probes import launch_chromium
        pw = self.FakePlaywright(set())
        with pytest.raises(RuntimeError) as excinfo:
            launch_chromium(pw)
        assert "chromium_headless_shell" in str(excinfo.value)

    def test_launch_kwargs_are_passed_through(self):
        from discovery.probes import launch_chromium
        captured = {}

        class Recorder(self.FakePlaywright):
            def launch(self, **kw):
                captured.update(kw)
                return super().launch(**kw)

        launch_chromium(Recorder({None}), headless=True)
        assert captured["headless"] is True

    def test_the_fallback_notice_is_printed_once(self, capsys):
        from discovery.probes import launch_chromium
        for _ in range(4):                      # the pass runs k probes
            launch_chromium(self.FakePlaywright({"chrome"}))
        assert capsys.readouterr().out.count("system chrome") == 1

    def test_channel_order_is_configurable(self, monkeypatch):
        import config
        from discovery.probes import launch_chromium
        monkeypatch.setattr(config, "BROWSER_CHANNELS", ["msedge", "chrome"])
        pw = self.FakePlaywright({"chrome", "msedge"})
        assert launch_chromium(pw)[1] == "msedge"
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

    @staticmethod
    def _fetch_with(exc, capsys):
        import requests

        from scrapers import robots

        original = requests.get
        requests.get = lambda *a, **k: (_ for _ in ()).throw(exc)
        try:
            return robots.RobotsCache()._fetch("https://example.com")
        finally:
            requests.get = original
            capsys.readouterr()

    def test_speculative_failures_are_silent(self, capsys):
        import requests

        from scrapers import robots

        original = requests.get
        requests.get = lambda *a, **k: (_ for _ in ()).throw(
            requests.exceptions.SSLError("handshake"))
        try:
            with robots.quiet():
                robots.RobotsCache()._fetch("https://red.io")
        finally:
            requests.get = original
        assert capsys.readouterr().out == ""

    def test_real_targets_still_report(self, capsys):
        import requests

        from scrapers import robots

        original = requests.get
        requests.get = lambda *a, **k: (_ for _ in ()).throw(
            requests.exceptions.SSLError("handshake"))
        try:
            robots.RobotsCache()._fetch("https://jobs.example.com")
        finally:
            requests.get = original
        assert "proceeding without restrictions" in capsys.readouterr().out

    def test_quiet_does_not_leak_past_its_block(self):
        from scrapers import robots
        with robots.quiet():
            pass
        assert robots._quiet_depth == 0

    def test_quiet_restores_on_exception(self):
        from scrapers import robots
        with pytest.raises(ValueError):
            with robots.quiet():
                raise ValueError("boom")
        assert robots._quiet_depth == 0, "a raising probe must not mute the crawl"

    def test_quiet_nests(self):
        from scrapers import robots
        with robots.quiet():
            with robots.quiet():
                assert robots._quiet_depth == 2
            assert robots._quiet_depth == 1, "the inner exit silenced the outer block"
        assert robots._quiet_depth == 0

    def test_quiet_never_changes_what_is_allowed(self, capsys):
        """Silence is a logging decision, not a politeness one."""
        import requests

        from scrapers import robots

        original = requests.get
        requests.get = lambda *a, **k: (_ for _ in ()).throw(
            requests.exceptions.SSLError("handshake"))
        try:
            with robots.quiet():
                rules = robots.RobotsCache()._fetch("https://red.io")
        finally:
            requests.get = original
            capsys.readouterr()
        assert rules.group is None and not rules.disallow_all   # still fails open
