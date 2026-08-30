"""Session logs: timestamped, levelled records of everything a run prints,
plus a file-only DEBUG channel for detail that is logged but never printed.

The conftest autouse fixture points core.session_log._log_dir at
tmp_path/"session-logs" for every test, so nothing here (or anywhere in the
suite) writes into the real data dir.
"""

import io
import logging
import re
import sys
import time
from datetime import datetime

import core.session_log as session_log

# One mirrored console record: "2026-08-28 09:30:00 LEVEL    console | msg"
_STAMP = r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}"


def _record(level, message):
    return re.compile(rf"^{_STAMP} {level:<8} console \| {re.escape(message)}$",
                      re.M)


class TestStartFinish:
    def test_mirrored_lines_carry_timestamp_and_level(
            self, monkeypatch, tmp_path):
        fake_out, fake_err = io.StringIO(), io.StringIO()
        monkeypatch.setattr(sys, "stdout", fake_out)
        monkeypatch.setattr(sys, "stderr", fake_err)

        path = session_log.start(["--track", "local", "--preview"],
                                 now=datetime(2026, 8, 28, 9, 30, 0))
        print("  12 new job(s)")
        print("  [!] a warning from stdout")
        print("stderr says hi", file=sys.stderr)
        session_log.finish()

        assert sys.stdout is fake_out, "finish() must restore stdout"
        assert sys.stderr is fake_err, "finish() must restore stderr"
        assert path.parent == tmp_path / "session-logs"
        assert path.name == "session-20260828-093000-crawl.log"

        text = path.read_text(encoding="utf-8")
        assert "# run     : run_scraper.py --track local --preview" in text
        assert "# started : 2026-08-28 09:30:00" in text
        assert "# ended   :" in text
        # every mirrored line is a record: timestamp, level, source, message
        assert _record("INFO", "  12 new job(s)").search(text)
        assert _record("WARNING", "  [!] a warning from stdout").search(text)
        assert _record("ERROR", "stderr says hi").search(text)

        # the tee copies — the console still sees the raw lines, unstamped
        assert "  12 new job(s)\n" in fake_out.getvalue()
        assert "stderr says hi" in fake_err.getvalue()
        assert not re.search(_STAMP, fake_out.getvalue()), \
            "console output must stay unstamped"

    def test_logged_detail_reaches_the_file_but_not_the_console(
            self, monkeypatch):
        fake_out = io.StringIO()
        monkeypatch.setattr(sys, "stdout", fake_out)
        monkeypatch.setattr(sys, "stderr", io.StringIO())

        path = session_log.start([], now=datetime(2026, 8, 28, 9, 30, 1))
        logging.getLogger("http").debug(
            "GET https://example.test/jobs -> %d in %.2fs", 200, 0.31)
        print("visible line")
        session_log.finish()

        text = path.read_text(encoding="utf-8")
        assert re.search(
            rf"^{_STAMP} DEBUG    http \| "
            rf"GET https://example\.test/jobs -> 200 in 0\.31s$",
            text, re.M), "debug records must land in the session file"
        assert "example.test" not in fake_out.getvalue(), \
            "debug records must never reach the console"
        assert "visible line" in fake_out.getvalue()

    def test_noisy_third_party_debug_is_capped(self, monkeypatch):
        monkeypatch.setattr(sys, "stdout", io.StringIO())
        monkeypatch.setattr(sys, "stderr", io.StringIO())
        path = session_log.start([], now=datetime(2026, 8, 28, 9, 30, 2))
        logging.getLogger("urllib3.connectionpool").debug("Starting new HTTPS")
        logging.getLogger("urllib3.connectionpool").warning("Retrying request")
        session_log.finish()
        text = path.read_text(encoding="utf-8")
        assert "Starting new HTTPS" not in text
        assert "Retrying request" in text

    def test_debug_is_allowlisted_to_app_loggers(self, monkeypatch):
        """The blocklist can't enumerate every dependency (ddgs' Rust HTTP
        bridge alone logs hyper_util/h2 frame traces at DEBUG, which flooded
        the 2026-08-28 discover-local log): DEBUG passes only from this
        app's own namespaces, INFO+ from anyone."""
        monkeypatch.setattr(sys, "stdout", io.StringIO())
        monkeypatch.setattr(sys, "stderr", io.StringIO())
        path = session_log.start([], now=datetime(2026, 8, 28, 9, 30, 5))
        logging.getLogger("hyper_util.client.legacy.pool").debug(
            "reuse idle connection")
        logging.getLogger("h2.codec.framed_write").debug("send frame=Headers")
        logging.getLogger("hyper_util.anything").info("third-party info line")
        logging.getLogger("scrapers.fetchers.workday").debug("app debug line")
        session_log.finish()
        text = path.read_text(encoding="utf-8")
        assert "reuse idle connection" not in text
        assert "send frame=Headers" not in text
        assert "third-party info line" in text
        assert "app debug line" in text

    def test_finish_detaches_the_handler_and_is_idempotent(self, monkeypatch):
        fake_out = io.StringIO()
        monkeypatch.setattr(sys, "stdout", fake_out)
        monkeypatch.setattr(sys, "stderr", io.StringIO())
        path = session_log.start([], now=datetime(2026, 8, 28, 9, 30, 3))
        print("during")
        session_log.finish()
        session_log.finish()
        print("after")
        logging.getLogger("http").debug("post-close record")
        text = path.read_text(encoding="utf-8")
        assert "during" in text
        assert "after" not in text
        assert "post-close record" not in text, \
            "close() must detach the file handler from the root logger"
        assert sys.stdout is fake_out


class TestThreadInterleaving:
    def test_concurrent_partial_writes_never_fuse_lines(
            self, monkeypatch, tmp_path):
        """print() writes text and newline separately; a worker thread's
        half-written line plus another thread's full line used to land in
        the file fused into one record (repeatedly seen in the 2026-08-28
        session logs). Lines are assembled per writing thread."""
        import threading
        monkeypatch.setattr(sys, "stdout", io.StringIO())
        monkeypatch.setattr(sys, "stderr", io.StringIO())
        path = session_log.start([], now=datetime(2026, 8, 28, 9, 30, 4))
        tee = sys.stdout                       # the installed tee
        wrote_partial = threading.Event()
        interloper_done = threading.Event()

        def worker():
            tee.write("worker-half ")          # no newline yet
            wrote_partial.set()
            interloper_done.wait(5)
            tee.write("worker-rest\n")

        t = threading.Thread(target=worker)
        t.start()
        assert wrote_partial.wait(5)
        tee.write("[!] interloper line\n")     # whole line, main thread
        interloper_done.set()
        t.join(5)
        session_log.finish()

        text = path.read_text(encoding="utf-8")
        assert _record("WARNING", "[!] interloper line").search(text)
        assert _record("INFO", "worker-half worker-rest").search(text)


class TestRetention:
    def test_oldest_logs_are_pruned_to_keep(self, tmp_path):
        d = tmp_path / "session-logs"
        d.mkdir(parents=True, exist_ok=True)
        for i in range(session_log.KEEP + 10):
            (d / f"session-20200101-{i:06d}-crawl.log").write_text("old")

        session_log.open_log("crawl", "run_scraper.py").close()

        logs = sorted(d.glob("session-*.log"))
        assert len(logs) == session_log.KEEP
        # the survivors are the newest: the earliest fakes are gone
        assert logs[0].name != "session-20200101-000000-crawl.log"


class TestWebappOps:
    def test_ui_op_output_lands_as_levelled_records(self, tmp_path):
        from webapp import ops

        def _op():
            print("probe-line")
            print("  [!] probe-warning")
            logging.getLogger("discovery").debug("probe debug detail")

        assert ops._run_op("probe", _op) is True
        while ops._running():
            time.sleep(0.02)
        time.sleep(0.15)          # let the worker's finally block land

        logs = list((tmp_path / "session-logs")
                    .glob("session-*-webui-probe.log"))
        assert len(logs) == 1
        text = logs[0].read_text(encoding="utf-8")
        assert "# run     : web UI op 'probe'" in text
        assert _record("INFO", "probe-line").search(text)
        assert _record("WARNING", "  [!] probe-warning").search(text)
        assert re.search(rf"^{_STAMP} DEBUG    discovery \| probe debug detail$",
                         text, re.M)
        assert "# ended   :" in text
