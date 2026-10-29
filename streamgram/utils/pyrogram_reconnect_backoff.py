# (c) adarsh-goel
"""Works around a reconnect-storm bug confirmed by reading pyrogram 2.0.106's own source
(pyrogram/session/session.py, Session.start): its retry loop on (OSError, RPCError) has no
delay at all between attempts -- it calls self.stop() and immediately loops back to try again.
Connection.connect() has its own small backoff, but only for a raw OSError during the initial
TCP handshake; a failure later in the loop (e.g. the post-connect Ping/InitConnection call
timing out, or any RPCError) skips that entirely and hits this unthrottled retry.

Confirmed live in this project's own logs: one incident produced 100,000+ reconnect attempts
in under 28 minutes (sustained 60+/second) pegging a CPU core the whole time, serving no real
traffic, until the process was hard-killed (a NUL-padded log file -- a mid-write kill, not a
clean stop) and needed a manual restart. This is exactly the "huge CPU, heavy restart" failure
mode reported against this deployment.

apply() patches Session.start to add capped exponential backoff before each retry (reset to
the floor on a successful connect). It's deliberately conservative about a future pyrogram
release changing these internals: every symbol it needs is resolved once, up front, inside the
same try/except that installs the patch, so any drift (a renamed method, a removed attribute)
surfaces immediately as a logged warning at startup -- falling back to the stock, unbounded-
retry behavior -- rather than as a crash the first time a session actually needs to reconnect.
"""
import asyncio
import logging

logger = logging.getLogger("streamgram.utils.pyrogram_reconnect_backoff")

_patched = False

BACKOFF_FLOOR = 1.0
BACKOFF_CAP = 30.0


def apply():
    global _patched
    if _patched:
        return
    try:
        _install_patch()
        _patched = True
        logger.info("Applied reconnect-backoff patch to pyrogram.session.Session.start")
    except Exception:
        logger.exception(
            "Could not apply the pyrogram reconnect-backoff patch (likely a pyrogram version "
            "mismatch) -- continuing with the stock Session.start, which has no retry backoff."
        )


def _install_patch():
    from pyrogram.session.session import Session

    # Resolved once, here, rather than inside the patched function: if any of these has moved
    # in a newer pyrogram release, that fails loudly right now (caught by apply()'s try/except)
    # instead of only surfacing the first time a session actually has to reconnect.
    Connection = Session.__init__.__globals__["Connection"]
    raw = Session.__init__.__globals__["raw"]
    layer = Session.__init__.__globals__["layer"]
    AuthKeyDuplicated = Session.__init__.__globals__["AuthKeyDuplicated"]
    RPCError = Session.__init__.__globals__["RPCError"]
    log = Session.__init__.__globals__["log"]

    async def start_with_backoff(self):
        backoff = BACKOFF_FLOOR
        while True:
            self.connection = Connection(
                self.dc_id, self.test_mode, self.client.ipv6, self.client.proxy, self.is_media
            )

            try:
                await self.connection.connect()

                self.recv_task = self.loop.create_task(self.recv_worker())

                await self.send(raw.functions.Ping(ping_id=0), timeout=self.START_TIMEOUT)

                if not self.is_cdn:
                    await self.send(
                        raw.functions.InvokeWithLayer(
                            layer=layer,
                            query=raw.functions.InitConnection(
                                api_id=await self.client.storage.api_id(),
                                app_version=self.client.app_version,
                                device_model=self.client.device_model,
                                system_version=self.client.system_version,
                                system_lang_code=self.client.lang_code,
                                lang_code=self.client.lang_code,
                                lang_pack="",
                                query=raw.functions.help.GetConfig(),
                            )
                        ),
                        timeout=self.START_TIMEOUT
                    )

                self.ping_task = self.loop.create_task(self.ping_worker())

                log.info("Session initialized: Layer %s", layer)
                log.info("Device: %s - %s", self.client.device_model, self.client.app_version)
                log.info("System: %s (%s)", self.client.system_version, self.client.lang_code)
            except AuthKeyDuplicated as e:
                await self.stop()
                raise e
            except (OSError, RPCError) as e:
                await self.stop()
                logger.warning(
                    "Session.start failed for dc_id=%s (%s) -- backing off %.1fs before retrying",
                    self.dc_id, e, backoff,
                )
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, BACKOFF_CAP)
            except Exception as e:
                await self.stop()
                raise e
            else:
                break

        self.is_started.set()
        log.info("Session started")

    Session.start = start_with_backoff
