# (c) adarsh-goel
"""Wraps the one connection failure that should end the process: the main bot client never
managing to reach Telegram at all.

wzgram (see requirements.txt) bounds a Session's own reconnect attempts to Session.MAX_RETRIES
(10) with capped exponential backoff and raises once they're exhausted, instead of stock
Pyrogram's zero-delay infinite retry -- the bug that produced a confirmed 28-minute, 100%-CPU
reconnect storm on this project's own server before this was fixed. That bound turns "stuck
retrying forever" into "fails after a bounded, measured ~4 minutes" (see
docs/04-deployment.md for how that figure was actually measured, not just estimated), which is
the real fix. What this module adds on top is narrower: once that failure does happen, nothing
downstream can run without the main bot connected, so the process is going to exit either way.
The one thing worth doing on the way out is making sure *why* is the loudest, clearest thing in
the log -- not a bare traceback three screens up from wherever pm2's log capture starts -- so
whoever next looks at `pm2 logs` or error.log doesn't have to reconstruct it. The actual
recovery policy (how many times to restart the whole process, and when to stop trying) belongs
to the process manager (pm2's max_restarts/min_uptime in process.json), not to more retrying
inside this process.
"""
import logging


def start_or_die(client, log):
    """Runs client.start() (Pyrogram/wzgram's async entrypoint for the main bot client) to
    completion via the client's own event loop, exactly how wzgram's own Client.run() does it
    internally (self.loop.run_until_complete(self.start())) -- not client.start() called bare.
    Stock Pyrogram used to make a bare call like that work on its own outside a running loop
    (detecting the context and running synchronously); wzgram does not reproduce that, so a
    bare call here would silently return an unawaited coroutine and do nothing at all, which is
    worse than any exception -- confirmed live the first time this file took the bare form.
    Driving the loop explicitly is correct for either library, so it isn't a wzgram-specific
    workaround to carry forward if the dependency ever changes again.

    On success, returns normally. On failure, logs one CRITICAL line explaining what happened
    and that the process is about to exit on purpose, flushes every logging handler (so that
    line actually reaches disk before the process dies -- a crash can otherwise race a buffered
    handler), and re-raises so the process exits non-zero. Never swallows the failure: letting
    it propagate is what makes this "fail as failure" rather than a silent hang.
    """
    try:
        client.loop.run_until_complete(client.start())
    except Exception:
        log.critical(
            "StreamBot could not connect to Telegram after exhausting its retries -- exiting so "
            "the process manager (pm2) can apply its own restart policy instead of this process "
            "hanging or retrying forever on its own.",
            exc_info=True,
        )
        logging.shutdown()
        raise
