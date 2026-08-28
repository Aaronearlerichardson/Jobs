"""Session logs: everything a run prints, mirrored to a file on disk.

The conftest autouse fixture points core.session_log._log_dir at
tmp_path/"session-logs" for every test, so nothing here (or anywhere in the
suite) writes into the real data dir.
"""

import io
import sys
import time
from datetime import datetime

import core.session_log as session_log


class TestStartFinish:
    def test_mirrors_stdout_and_stderr_and_stamps_header_footer(
            self, monkeypatch, tmp_path):
        fake_out, fake_err = io.StringIO(), io.StringIO()
        monkeypatch.setattr(sys, "stdout", fake_out)
        monkeypatch.setattr(sys, "stderr", fake_err)

        path = session_log.start(["--track", "local", "--preview"],
                                 now=datetime(2026, 8, 28, 9, 30, 0))
        print("  12 new job(s)")
        print("  [!] a warning", file=sys.stderr)
        session_log.finish()

        assert sys.stdout is fake_out, "finish() must restore stdout"
        assert sys.stderr is fake_err, "finish() must restore stderr"
        assert path.parent == tmp_path / "session-logs"
        assert path.name == "session-20260828-093000-crawl.log"

        text = path.read_text(encoding="utf-8")
        assert "# run     : run_scraper.py --track local --preview" in text
        assert "# started : 2026-08-28 09:30:00" in text
        assert "12 new job(s)" in text
        assert "[!] a warning" in text, "stderr must be captured too"
        assert "# ended   :" in text

        # The tee copies — the console still sees everything.
        assert "12 new job(s)" in fake_out.getvalue()
        assert "a warning" in fake_err.getvalue()

    def test_finish_twice_is_harmless(self, monkeypatch):
        fake_out = io.StringIO()
        monkeypatch.setattr(sys, "stdout", fake_out)
        monkeypatch.setattr(sys, "stderr", io.StringIO())
        session_log.start([], now=datetime(2026, 8, 28, 9, 30, 1))
        session_log.finish()
        session_log.finish()
        assert sys.stdout is fake_out

    def test_prints_after_finish_do_not_reach_the_log(self, monkeypatch):
        monkeypatch.setattr(sys, "stdout", io.StringIO())
        monkeypatch.setattr(sys, "stderr", io.StringIO())
        path = session_log.start([], now=datetime(2026, 8, 28, 9, 30, 2))
        print("during")
        session_log.finish()
        print("after")
        text = path.read_text(encoding="utf-8")
        assert "during" in text
        assert "after" not in text


class TestRetention:
    def test_oldest_logs_are_pruned_to_keep(self, tmp_path):
        d = tmp_path / "session-logs"
        d.mkdir(parents=True, exist_ok=True)
        for i in range(session_log.KEEP + 10):
            (d / f"session-20200101-{i:06d}-crawl.log").write_text("old")

        _, fh = session_log.open_log("crawl", "run_scraper.py")
        fh.close()

        logs = sorted(d.glob("session-*.log"))
        assert len(logs) == session_log.KEEP
        # the survivors are the newest: the earliest fakes are gone
        assert logs[0].name != "session-20200101-000000-crawl.log"


class TestWebappOps:
    def test_ui_op_output_lands_in_a_session_log(self, tmp_path):
        from webapp import ops

        assert ops._run_op("probe", lambda: print("probe-line")) is True
        while ops._running():
            time.sleep(0.02)
        time.sleep(0.15)          # let the worker's finally block land

        logs = list((tmp_path / "session-logs")
                    .glob("session-*-webui-probe.log"))
        assert len(logs) == 1
        text = logs[0].read_text(encoding="utf-8")
        assert "# run     : web UI op 'probe'" in text
        assert "probe-line" in text
        assert "# ended   :" in text
