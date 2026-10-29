"""Tests for the wzgram migration: that the package actually installed is what we think it is
and still provides the one behavior this whole migration exists for (bounded, backed-off
retries instead of stock Pyrogram's zero-delay infinite reconnect loop -- the bug that produced
a confirmed 28-minute, 100%-CPU incident on this project's own server), and that a real,
unrecoverable connection failure in the main bot actually ends the process instead of hanging or
retrying forever inside it -- "fail as failure", so pm2's own restart policy (process.json) can
take over.

W2 makes a real (unmocked) TCP connection to a closed local port and waits for it to
genuinely exhaust its retries -- deliberately, not a mistake, since the retry/backoff logic
living three layers deep in Connection.connect() is exactly the thing a mock would paper over
if the whole point is making sure it's not a zero-delay spin anymore. It's slow (several tens
of seconds) by nature; that's the cost of it meaning something.

Run:  python tests/test_wzgram_migration.py   (or: pytest tests)
"""
import asyncio
import json
import logging
import os
import socket
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from pyrogram import Client  # noqa: E402
from pyrogram.session.session import Session  # noqa: E402
from streamgram.utils.fatal_startup import start_or_die  # noqa: E402


class FakeLog:
    """Stands in for a logging.Logger: records what start_or_die reports, without needing
    pytest's caplog fixture, so this file stays runnable as a plain script too."""
    def __init__(self):
        self.critical_calls = []

    def critical(self, msg, *args, **kwargs):
        self.critical_calls.append((msg, args, kwargs))


class FakeClient:
    """Mirrors the real shape start_or_die actually drives -- client.start() returns a
    coroutine (it's `async def`, exactly like the real Pyrogram/wzgram method), and
    client.loop.run_until_complete(...) is what actually runs it and surfaces the result or
    the exception. A fake .start() that was just a plain sync method would pass even a version
    of start_or_die that calls client.start() bare -- the exact bug this file caught for
    real (a bare call silently no-ops against wzgram, awaiting nothing) -- so it would prove
    nothing. Using a real event loop here, not a further mock of run_until_complete itself, is
    what makes W3 a genuine test of the calling convention, not just of our own bookkeeping."""
    def __init__(self, succeed):
        self._succeed = succeed
        self.started = False
        self.loop = asyncio.new_event_loop()

    async def start(self):
        self.started = True
        if not self._succeed:
            raise ConnectionError("simulated: could not reach Telegram")


def closed_local_port():
    """A port on 127.0.0.1 nothing is listening on: connecting to it fails fast (refused)
    rather than timing out slowly, so the real-network test below finishes in a reasonable time
    while still exercising genuine OS-level connection failures, not a mocked exception."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def test_wzgram_migration():
    # ---- W1: the package actually installed is wzgram, aliased onto the pyrogram name, with
    # the specific behavior this migration depends on still present. This is a tripwire: if a
    # future wzgram release (or an accidental `pip install pyrogram` undoing the swap) removes
    # or renames any of this, this test fails loudly at the one place it matters, instead of the
    # next real outage silently behaving like the old, unbounded-retry bug again.
    import pyrogram
    import wzgram
    assert wzgram is pyrogram, "wzgram should alias the pyrogram module object exactly, not wrap it"
    assert pyrogram.__version__ == "3.1.6", pyrogram.__version__
    assert Session.MAX_RETRIES >= 1, "the retry bound this migration exists for is gone"
    import inspect
    params = inspect.signature(Session.start).parameters
    assert "max_attempts" in params, "Session.start no longer accepts max_attempts -- the bounded-retry fix this migration exists for may have been removed upstream"
    import warpcrypto  # noqa: F401 -- just confirming it's importable; the old tgcrypto was silently absent in every log this project had
    print("W1 ok: wzgram is installed and aliased as pyrogram, with max_attempts and warpcrypto present")

    # ---- W2: a session that can never connect backs off between real attempts and eventually
    # raises -- it does not hang, and it does not spin. Uses a tiny max_attempts so the test
    # finishes in seconds rather than the ~several-minute worst case a production max_attempts
    # (Session.MAX_RETRIES, 10) would take; the backoff formula (capped exponential) is the same
    # either way, so this still proves the real thing, just on a smaller N.
    async def run_w2():
        client = Client(name="test-w2", api_id=1, api_hash="x", in_memory=True)
        port = closed_local_port()
        session = Session(
            client, dc_id=1, auth_key=b"\x00" * 256, test_mode=False,
            is_media=False, is_cdn=True,  # is_cdn=True skips the InvokeWithLayer/storage.api_id() call
            server_address="127.0.0.1", port=port,
        )
        start = time.monotonic()
        try:
            await session.start(max_attempts=3)
            raise AssertionError("connecting to a closed port should not succeed")
        except AssertionError:
            raise
        except Exception as e:
            elapsed = time.monotonic() - start
            return elapsed, e

    elapsed, exc = asyncio.run(run_w2())
    # Session-level backoff alone between 3 attempts is 1 + 2 = 3s (capped exponential, floor
    # 1s); Connection.connect()'s own inner retry (3 sub-attempts, 1s apart, unchanged from
    # stock Pyrogram) adds more under *every* Session-level attempt. The floor below is just
    # "this did not spin with zero delay" -- the old bug would fail this in well under a second.
    assert elapsed >= 2.5, f"expected real backoff delay, only took {elapsed:.2f}s -- looks like a zero-delay spin"
    assert elapsed < 120, f"expected bounded retries to finish well within a couple minutes, took {elapsed:.1f}s -- looks hung"
    assert not isinstance(exc, (AssertionError,))
    print(f"W2 ok: a truly unreachable connection backed off for real ({elapsed:.1f}s for 3 attempts) and then raised ({type(exc).__name__}), not hung and not instant")

    # ---- W3: start_or_die -- the one thing this project adds on top of wzgram's own fix.
    # Success path: no log, no raise.
    log = FakeLog()
    client_ok = FakeClient(succeed=True)
    start_or_die(client_ok, log)
    assert client_ok.started
    assert log.critical_calls == []
    print("W3a ok: start_or_die is silent and doesn't raise when the client connects fine")

    # Failure path: logs exactly one CRITICAL line with exc_info for the real traceback, and
    # the original exception still propagates -- "fail as failure", never swallowed, since a
    # swallowed failure here is a silently-dead bot pm2 has no reason to restart.
    log = FakeLog()
    client_bad = FakeClient(succeed=False)
    try:
        start_or_die(client_bad, log)
        raise AssertionError("start_or_die must re-raise the connection failure")
    except AssertionError:
        raise
    except ConnectionError as e:
        assert "simulated" in str(e)
    assert client_bad.started
    assert len(log.critical_calls) == 1, log.critical_calls
    msg, args, kwargs = log.critical_calls[0]
    assert "exiting" in msg and "process manager" in msg, msg
    assert kwargs.get("exc_info") is True, "must capture the real traceback, not just the summary line"
    print("W3b ok: start_or_die logs one clear CRITICAL line (with exc_info) and still re-raises -- the process will actually exit")

    # logging.shutdown() must run before start_or_die re-raises, not after -- otherwise a crash
    # on the way out could beat a buffered handler to disk and the CRITICAL line never lands
    # where pm2 logs/error.log can show it.
    shutdown_calls = []
    original_shutdown = logging.shutdown
    logging.shutdown = lambda: shutdown_calls.append(True)
    try:
        log = FakeLog()
        try:
            start_or_die(FakeClient(succeed=False), log)
        except ConnectionError:
            pass
        assert shutdown_calls == [True], "logging.shutdown() should run exactly once, before re-raising"
    finally:
        logging.shutdown = original_shutdown
    print("W3c ok: logging.shutdown() runs before the exception propagates, so the CRITICAL line is flushed to disk first")

    # ---- W4: process.json's min_uptime must stay above the worst-case time a doomed main-bot
    # connection spends retrying before start_or_die lets it die -- otherwise pm2 never counts
    # that as a failed start (see docs/04-deployment.md's explanation of why), and a *persistent*
    # outage just restarts forever, slowly, instead of pm2 eventually giving up and surfacing it.
    # This computes a theoretical floor from the two retry layers' own constants (deliberate
    # sleeps only -- Connection.connect()'s 1s inter-attempt sleep is a literal in its source,
    # not an exposed constant, so it's reproduced here as a comment-documented assumption, not
    # read reflectively); the real number measured by W2's approach, extrapolated to
    # Session.MAX_RETRIES attempts, is additionally slower, so this floor is conservative on
    # purpose -- it exists to catch process.json drifting *below* a safe value, not to pin the
    # exact figure.
    from pyrogram.connection.connection import Connection
    session_backoff_sum = sum(min(2 ** (a - 1), 30) for a in range(1, Session.MAX_RETRIES))
    connection_layer_sleep_per_attempt = (Connection.MAX_CONNECTION_ATTEMPTS - 1) * 1  # 1s between sub-attempts
    theoretical_floor = session_backoff_sum + connection_layer_sleep_per_attempt * Session.MAX_RETRIES

    process_json = json.load(open(os.path.join(ROOT, "process.json")))
    app = process_json["apps"][0]
    min_uptime = app["min_uptime"]
    min_uptime_seconds = int(min_uptime[:-1]) * 60 if min_uptime.endswith("m") else int(min_uptime)
    assert min_uptime_seconds > theoretical_floor, (
        f"process.json min_uptime ({min_uptime_seconds}s) is below the theoretical worst-case "
        f"retry time (~{theoretical_floor}s) -- pm2 would stop counting a doomed main bot's exit "
        f"as a failed start, and it would just restart forever instead of eventually giving up"
    )
    assert app.get("max_restarts", 1) >= 1, "max_restarts must allow at least one real retry"
    print(f"W4 ok: process.json min_uptime ({min_uptime_seconds}s) comfortably exceeds the theoretical worst-case retry floor (~{theoretical_floor}s)")


if __name__ == "__main__":
    test_wzgram_migration()
    print("ALL OK")
