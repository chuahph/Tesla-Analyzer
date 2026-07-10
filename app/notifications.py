"""Web push notifications (charge complete, low battery, ...).

Uses the ``webpush`` package (pure cryptography + pydantic + PyJWT — no
build-time C-extension dependencies, unlike pywebpush's http-ece
requirement, which fails to build on current Python/setuptools). Disabled
entirely unless both VAPID keys are configured; every function here is a
safe no-op in that case so the sync loop never has to check "is this
enabled" itself.
"""
from __future__ import annotations

from datetime import datetime

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import get_settings
from .models import PushSubscription


def _pem(value: str) -> bytes:
    """Reconstitute a PEM's real newlines from the single-line \\n-escaped
    form the keys are stored as in an env var (see run.py's push-keys)."""
    return value.replace("\\n", "\n").encode()


def enabled(settings=None) -> bool:
    settings = settings or get_settings()
    return bool(settings.vapid_private_key_pem.strip() and settings.vapid_public_key_pem.strip())


def public_key_b64(settings=None) -> str | None:
    """The browser-facing VAPID application server key (base64url), for
    PushManager.subscribe({applicationServerKey: ...})."""
    settings = settings or get_settings()
    if not enabled(settings):
        return None
    from webpush.vapid import VAPID

    v = VAPID(private_key=_pem(settings.vapid_private_key_pem),
              public_key=_pem(settings.vapid_public_key_pem))
    return VAPID.get_application_server_key(v.public_key)


def subscribe(session: Session, endpoint: str, p256dh: str, auth: str) -> None:
    """Upsert a subscription by endpoint (a browser re-subscribing after
    e.g. clearing storage sends the same endpoint again)."""
    existing = session.scalars(
        select(PushSubscription).where(PushSubscription.endpoint == endpoint)
    ).first()
    if existing:
        existing.p256dh, existing.auth = p256dh, auth
    else:
        session.add(PushSubscription(
            endpoint=endpoint, p256dh=p256dh, auth=auth, created_at=datetime.now()))
    session.commit()


def unsubscribe(session: Session, endpoint: str) -> None:
    session.query(PushSubscription).filter(PushSubscription.endpoint == endpoint).delete()
    session.commit()


def notify(session: Session, title: str, body: str, tag: str | None = None) -> int:
    """Send a notification to every subscribed device. Returns how many
    subscriptions were actually delivered to. A no-op (returns 0) when
    push isn't configured or nobody's subscribed — safe to call
    unconditionally from the sync loop.

    Expired/invalid subscriptions (the push service returns 404/410) are
    deleted so the subscriber list stays clean without a separate sweep.
    """
    settings = get_settings()
    if not enabled(settings):
        return 0
    subs = session.scalars(select(PushSubscription)).all()
    if not subs:
        return 0

    from webpush import WebPush
    from webpush.types import WebPushKeys, WebPushSubscription

    wp = WebPush(
        private_key=_pem(settings.vapid_private_key_pem),
        public_key=_pem(settings.vapid_public_key_pem),
        subscriber=settings.vapid_subject_email,
    )
    payload = {"title": title, "body": body, "tag": tag or "tesla-analyzer"}

    sent = 0
    stale_ids: list[int] = []
    for sub in subs:
        subscription = WebPushSubscription(
            endpoint=sub.endpoint,
            keys=WebPushKeys(p256dh=sub.p256dh, auth=sub.auth),
        )
        try:
            message = wp.get(payload, subscription)
            resp = httpx.post(
                str(subscription.endpoint), content=message.encrypted,
                headers=dict(message.headers), timeout=10.0,
            )
            if resp.status_code in (404, 410):
                stale_ids.append(sub.id)
            elif resp.status_code < 300:
                sent += 1
        except Exception:  # noqa: BLE001 — one bad subscription must not
            # block notifying the rest, or block whatever sync-loop event
            # triggered this call.
            continue

    if stale_ids:
        session.query(PushSubscription).filter(PushSubscription.id.in_(stale_ids)).delete(
            synchronize_session=False)
        session.commit()
    return sent
