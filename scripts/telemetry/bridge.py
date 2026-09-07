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

Failure policy is "keep the car's data, lose the batch": a POST that fails is
retried a few times and then dropped with a log line. Blocking here would back
up the ZMQ queue and eventually make the server refuse the vehicle's
connection, which is a far worse outcome than a gap the app can see.
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


def post(records: list) -> None:
    """POST one batch, retrying briefly, then giving up loudly."""
    if not records or not APP_URL:
        return
    url = f"{APP_URL}/api/telemetry"
    params = {"key": SYNC_KEY} if SYNC_KEY else {}
    for attempt in range(1, POST_RETRIES + 1):
        try:
            resp = requests.post(
                url, params=params, json={"records": records}, timeout=POST_TIMEOUT
            )
            if resp.status_code < 300:
                LOG.info("posted %d record(s)", len(records))
                return
            # 4xx will not fix itself on retry — a wrong key or a missing
            # endpoint is a deployment problem, not a transient one.
            if 400 <= resp.status_code < 500:
                LOG.error("app rejected batch: HTTP %d %s",
                          resp.status_code, resp.text[:200])
                return
            LOG.warning("attempt %d: HTTP %d", attempt, resp.status_code)
        except requests.RequestException as exc:
            LOG.warning("attempt %d: %s", attempt, exc)
        time.sleep(2 ** attempt)
    LOG.error("dropping %d record(s) after %d attempts", len(records), POST_RETRIES)


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
    last_flush = time.monotonic()
    while True:
        events = dict(poller.poll(timeout=1000))
        if sock in events:
            record = parse(sock.recv_multipart())
            if record is not None:
                batch.append(record)
        due = time.monotonic() - last_flush >= BATCH_SECONDS
        if batch and (len(batch) >= BATCH_MAX or due):
            post(batch)
            batch = []
            last_flush = time.monotonic()


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(0)
