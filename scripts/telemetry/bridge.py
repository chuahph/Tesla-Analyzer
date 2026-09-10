#!/usr/bin/env python3
"""Forward Fleet Telemetry records from fleet-telemetry's ZMQ socket to the app.

The car streams to a fleet-telemetry server on this box; that server publishes
each record on a ZMQ PUB socket (as JSON, because the config sets
``transmit_decoded_records``). This bridge subscribes, batches, and POSTs to
``/api/telemetry`` on the app.

Deliberately dumb. It does no interpretation at all: it does not merge fields
into a snapshot, does not decide what a trip is, does not drop anything it
doesn't recognise. All of that lives in the app, where it is tested and version
controlled — this process runs on a box nobody looks at, so the less judgement
it exercises the fewer places a wrong judgement can hide.

Failure policy is "never block the car, never lose the batch". Blocking here
would back up the ZMQ queue and eventually make the server refuse the
vehicle's connection, which is worse than any gap — so a POST that fails is
retried briefly and then written to a small disk spool, and drained on a
later flush.

The spool is the one thing on this box that keeps data, and it is worth being
clear about why that does not contradict "the VM stores nothing". The car
buffers what it cannot send and replays it in order — measured, a 35-minute
drive arrived 36 minutes late and rebuilt to within 0.4%. That covers the
car-to-here link completely. Nothing covered here-to-the-app: the car has
already handed the records over and will never send them again, so a batch
dropped at this point is gone in a way no other failure here is. The spool is
a transient buffer for that one hop, not storage.

Drained BEFORE the current batch, always. Spooled records are older than
live ones, and the app builds a trip from a stream it reads in order — send
the new batch first and the recovered one arrives looking like the odometer
running backwards, which is exactly the state it refuses. If the drain
cannot complete, the live batch joins the spool behind it rather than
overtaking it.
"""
from __future__ import annotations

import json
import logging
import os
import sys
import time

import requests
import zmq

LOG = logging.getLogger("bridge")

ZMQ_ADDR = os.environ.get("ZMQ_ADDR", "tcp://127.0.0.1:5284")
# fleet-telemetry binds its PUB socket, so the default here is to connect.
# Kept configurable because that is an implementation detail of the server and
# not something its docs promise — if it ever connects instead, this flips to
# "bind" without a code change.
ZMQ_MODE = os.environ.get("ZMQ_MODE", "connect")
APP_URL = os.environ.get("APP_URL", "").rstrip("/")
SYNC_KEY = os.environ.get("SYNC_KEY", "")

# Batching: the app is on a free web host, so a POST per record would be both
# wasteful and slow. A few seconds of latency costs nothing — the point of
# telemetry is that the car reports events we would otherwise never see, not
# that we see them within a second.
BATCH_MAX = int(os.environ.get("BATCH_MAX", "50"))
BATCH_SECONDS = float(os.environ.get("BATCH_SECONDS", "5"))
POST_TIMEOUT = float(os.environ.get("POST_TIMEOUT", "30"))
POST_RETRIES = int(os.environ.get("POST_RETRIES", "3"))

# Where batches wait when the app cannot be reached. /var/tmp because it
# survives a reboot and needs no setup step on a box nobody logs into.
SPOOL_PATH = os.environ.get("SPOOL_PATH", "/var/tmp/tesla-bridge-spool.jsonl")
# An hour of a parked car is about 180 batches, a day about 4,000. The cap is
# generous enough that any outage worth recovering from fits, and finite so a
# permanently misconfigured APP_URL cannot fill the disk.
SPOOL_MAX_BATCHES = int(os.environ.get("SPOOL_MAX_BATCHES", "20000"))
# How many spooled batches one flush may send. The loop must stay responsive
# to ZMQ, and POST_TIMEOUT is 30 seconds, so a long backlog drains steadily
# over many cycles rather than stalling the process on one.
SPOOL_DRAIN_MAX = int(os.environ.get("SPOOL_DRAIN_MAX", "5"))
# Drain even when the car is silent: a backlog written during an outage would
# otherwise sit there until the next record arrives, which for a sleeping car
# is the next morning.
DRAIN_SECONDS = float(os.environ.get("DRAIN_SECONDS", "30"))


def spool(records: list) -> None:
    """Keep a batch the app would not take, oldest first."""
    if not records:
        return
    try:
        lines = []
        if os.path.exists(SPOOL_PATH):
            with open(SPOOL_PATH, encoding="utf-8") as fh:
                lines = fh.read().splitlines()
        lines.append(json.dumps(records))
        if len(lines) > SPOOL_MAX_BATCHES:
            # Keep the newest, drop the oldest. A trip is rebuilt from the
            # records around it, so the most recent hours are the ones still
            # worth recovering; the far end of a multi-day outage is not.
            dropped = len(lines) - SPOOL_MAX_BATCHES
            lines = lines[-SPOOL_MAX_BATCHES:]
            LOG.error("spool full: dropped %d oldest batch(es)", dropped)
        with open(SPOOL_PATH, "w", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")
        LOG.warning("spooled %d record(s); %d batch(es) waiting",
                    len(records), len(lines))
    except OSError as exc:
        LOG.error("could not spool %d record(s): %s", len(records), exc)


def drain() -> bool:
    """Send what the spool is holding, oldest first. True when it is empty.

    Stops at the first batch the app will not take and keeps the rest, so a
    partial drain never reorders what is left.
    """
    try:
        if not os.path.exists(SPOOL_PATH):
            return True
        with open(SPOOL_PATH, encoding="utf-8") as fh:
            lines = [ln for ln in fh.read().splitlines() if ln.strip()]
    except OSError as exc:
        LOG.error("could not read spool: %s", exc)
        return False
    if not lines:
        return True

    sent = 0
    for line in lines[:SPOOL_DRAIN_MAX]:
        try:
            records = json.loads(line)
        except json.JSONDecodeError:
            LOG.error("discarding unreadable spool line (%d bytes)", len(line))
            sent += 1                     # unreadable is not retryable
            continue
        if not post(records):
            break
        sent += 1

    remaining = lines[sent:]
    try:
        if remaining:
            with open(SPOOL_PATH, "w", encoding="utf-8") as fh:
                fh.write("\n".join(remaining) + "\n")
        else:
            os.remove(SPOOL_PATH)
    except OSError as exc:
        LOG.error("could not rewrite spool: %s", exc)
        return False
    if sent:
        LOG.info("drained %d batch(es); %d left", sent, len(remaining))
    return not remaining


def send(records: list) -> None:
    """Deliver one live batch, in order, spooling rather than dropping."""
    if not records:
        return
    if not drain():
        # Something older is still stuck. This batch goes behind it, because
        # arriving first would put the app's stream out of order.
        spool(records)
        return
    if not post(records):
        spool(records)


def post(records: list) -> bool:
    """POST one batch, retrying briefly. False means it did not get through."""
    if not records or not APP_URL:
        return True
    url = f"{APP_URL}/api/telemetry"
    params = {"key": SYNC_KEY} if SYNC_KEY else {}
    for attempt in range(1, POST_RETRIES + 1):
        try:
            resp = requests.post(
                url, params=params, json={"records": records}, timeout=POST_TIMEOUT
            )
            if resp.status_code < 300:
                LOG.info("posted %d record(s)", len(records))
                return True
            # 4xx will not fix itself on retry — a wrong key or a missing
            # endpoint is a deployment problem, not a transient one, and
            # spooling it would build a backlog that can never drain.
            if 400 <= resp.status_code < 500:
                LOG.error("app rejected batch: HTTP %d %s",
                          resp.status_code, resp.text[:200])
                return True
            LOG.warning("attempt %d: HTTP %d", attempt, resp.status_code)
        except requests.RequestException as exc:
            LOG.warning("attempt %d: %s", attempt, exc)
        time.sleep(2 ** attempt)
    LOG.warning("app unreachable after %d attempts; spooling %d record(s)",
                POST_RETRIES, len(records))
    return False


def parse(frames: list) -> dict | None:
    """Pull the JSON payload out of a ZMQ message.

    The publisher sends [topic, payload]; older builds send the payload alone.
    Anything that is not JSON is logged and skipped rather than crashing the
    bridge — an unfamiliar record type must not be able to stop the stream.
    """
    raw = frames[-1] if frames else b""
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        LOG.warning("skipping non-JSON frame (%d bytes)", len(raw))
        return None


def main() -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
    if not APP_URL:
        LOG.error("APP_URL is not set — nothing to forward to")
        return 1

    ctx = zmq.Context()
    sock = ctx.socket(zmq.SUB)
    if ZMQ_MODE == "bind":
        sock.bind(ZMQ_ADDR)
    else:
        sock.connect(ZMQ_ADDR)
    sock.setsockopt(zmq.SUBSCRIBE, b"")  # every topic
    LOG.info("%s %s -> %s/api/telemetry", ZMQ_MODE, ZMQ_ADDR, APP_URL)

    poller = zmq.Poller()
    poller.register(sock, zmq.POLLIN)

    batch: list = []
    last_flush = last_drain = time.monotonic()
    while True:
        events = dict(poller.poll(timeout=1000))
        if sock in events:
            record = parse(sock.recv_multipart())
            if record is not None:
                batch.append(record)
        now = time.monotonic()
        if batch and (len(batch) >= BATCH_MAX or now - last_flush >= BATCH_SECONDS):
            send(batch)
            batch = []
            last_flush = last_drain = time.monotonic()
        elif now - last_drain >= DRAIN_SECONDS:
            # A backlog written while the app was down would otherwise wait
            # for the next record, which for a sleeping car is tomorrow.
            drain()
            last_drain = time.monotonic()


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(0)
