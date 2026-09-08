"""Tests for app/notifications.py (web push)."""
from types import SimpleNamespace

import httpx

from app import notifications
from app.models import PushSubscription


def _settings(**overrides):
    base = dict(
        vapid_private_key_pem="", vapid_public_key_pem="",
        vapid_subject_email="test@example.com",
        event_webhook_url="",
        telegram_bot_token="", telegram_chat_id="",
        whatsapp_phone="", whatsapp_apikey="",
        ntfy_topic="", ntfy_server="https://ntfy.sh",
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _vapid_keys():
    from webpush.vapid import VAPID

    priv, pub, appkey = VAPID.generate_keys()
    return (priv.decode().strip().replace("\n", "\\n"),
            pub.decode().strip().replace("\n", "\\n"), appkey)


def _fake_browser_keys():
    """A syntactically valid p256dh/auth pair (a real EC point + random
    bytes) so the encryption path runs for real rather than raising."""
    import base64
    import os as _os

    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

    client_key = ec.generate_private_key(ec.SECP256R1())
    pub_bytes = client_key.public_key().public_bytes(Encoding.X962, PublicFormat.UncompressedPoint)
    p256dh = base64.urlsafe_b64encode(pub_bytes).rstrip(b"=").decode()
    auth = base64.urlsafe_b64encode(_os.urandom(16)).rstrip(b"=").decode()
    return p256dh, auth


def test_disabled_without_both_vapid_keys():
    assert notifications.enabled(_settings()) is False
    priv, pub, _ = _vapid_keys()
    assert notifications.enabled(_settings(vapid_private_key_pem=priv)) is False
    assert notifications.enabled(_settings(vapid_public_key_pem=pub)) is False
    assert notifications.enabled(_settings(vapid_private_key_pem=priv, vapid_public_key_pem=pub)) is True


def test_public_key_b64_matches_generated_app_key():
    priv, pub, appkey = _vapid_keys()
    settings = _settings(vapid_private_key_pem=priv, vapid_public_key_pem=pub)
    assert notifications.public_key_b64(settings) == appkey
    assert notifications.public_key_b64(_settings()) is None


def test_subscribe_upserts_by_endpoint(session):
    p256dh, auth = _fake_browser_keys()
    notifications.subscribe(session, "https://push.example.com/a", p256dh, auth)
    assert session.query(PushSubscription).count() == 1

    # Re-subscribing the same endpoint updates in place, not a duplicate row.
    p256dh2, auth2 = _fake_browser_keys()
    notifications.subscribe(session, "https://push.example.com/a", p256dh2, auth2)
    assert session.query(PushSubscription).count() == 1
    row = session.query(PushSubscription).first()
    assert row.p256dh == p256dh2

    notifications.unsubscribe(session, "https://push.example.com/a")
    assert session.query(PushSubscription).count() == 0


def test_notify_is_a_noop_when_disabled_or_no_subscribers(session, monkeypatch):
    monkeypatch.setattr("app.notifications.get_settings", lambda: _settings())
    assert notifications.notify(session, "t", "b") == 0

    priv, pub, _ = _vapid_keys()
    monkeypatch.setattr(
        "app.notifications.get_settings",
        lambda: _settings(vapid_private_key_pem=priv, vapid_public_key_pem=pub),
    )
    assert notifications.notify(session, "t", "b") == 0   # enabled, but nobody subscribed


def test_notify_sends_to_each_subscriber_and_prunes_stale_ones(session, monkeypatch):
    priv, pub, _ = _vapid_keys()
    monkeypatch.setattr(
        "app.notifications.get_settings",
        lambda: _settings(vapid_private_key_pem=priv, vapid_public_key_pem=pub),
    )
    p1, a1 = _fake_browser_keys()
    p2, a2 = _fake_browser_keys()
    notifications.subscribe(session, "https://push.example.com/live", p1, a1)
    notifications.subscribe(session, "https://push.example.com/gone", p2, a2)

    calls = []

    def fake_post(url, content=None, headers=None, timeout=None):
        calls.append(url)
        status = 410 if "gone" in url else 201
        return httpx.Response(status, request=httpx.Request("POST", url))

    monkeypatch.setattr("app.notifications.httpx.post", fake_post)

    sent = notifications.notify(session, "Charging complete", "1.5 kWh added")
    assert sent == 1   # only the "live" endpoint counted as delivered
    assert len(calls) == 2
    # The 410 (gone) subscription was pruned; the live one remains.
    remaining = [s.endpoint for s in session.query(PushSubscription).all()]
    assert remaining == ["https://push.example.com/live"]


def test_notify_survives_one_bad_subscription(session, monkeypatch):
    """A network error delivering to one subscriber must not stop the rest
    from being notified."""
    priv, pub, _ = _vapid_keys()
    monkeypatch.setattr(
        "app.notifications.get_settings",
        lambda: _settings(vapid_private_key_pem=priv, vapid_public_key_pem=pub),
    )
    p1, a1 = _fake_browser_keys()
    p2, a2 = _fake_browser_keys()
    notifications.subscribe(session, "https://push.example.com/broken", p1, a1)
    notifications.subscribe(session, "https://push.example.com/ok", p2, a2)

    def flaky_post(url, content=None, headers=None, timeout=None):
        if "broken" in url:
            raise httpx.ConnectError("refused", request=httpx.Request("POST", url))
        return httpx.Response(201, request=httpx.Request("POST", url))

    monkeypatch.setattr("app.notifications.httpx.post", flaky_post)
    assert notifications.notify(session, "t", "b") == 1


def test_fire_webhook_noop_without_url(monkeypatch):
    monkeypatch.setattr("app.notifications.get_settings", lambda: _settings())
    calls = []
    monkeypatch.setattr("app.notifications.httpx.post", lambda *a, **k: calls.append((a, k)))
    assert notifications.fire_webhook("charge-complete", "t", "b") is False
    assert calls == []


def test_fire_webhook_posts_json_payload(monkeypatch):
    monkeypatch.setattr(
        "app.notifications.get_settings",
        lambda: _settings(event_webhook_url="https://example.invalid/hook"),
    )
    sent = {}

    def fake_post(url, json=None, timeout=None):
        sent["url"], sent["json"] = url, json
        return httpx.Response(200, request=httpx.Request("POST", url))

    monkeypatch.setattr("app.notifications.httpx.post", fake_post)
    assert notifications.fire_webhook("charge-complete", "Charging complete", "1.5 kWh added") is True
    assert sent["url"] == "https://example.invalid/hook"
    assert sent["json"]["event"] == "charge-complete"
    assert sent["json"]["title"] == "Charging complete"
    assert sent["json"]["body"] == "1.5 kWh added"
    assert "timestamp" in sent["json"]


def test_fire_webhook_survives_delivery_failure(monkeypatch):
    monkeypatch.setattr(
        "app.notifications.get_settings",
        lambda: _settings(event_webhook_url="https://example.invalid/hook"),
    )

    def failing_post(url, json=None, timeout=None):
        raise httpx.ConnectError("refused", request=httpx.Request("POST", url))

    monkeypatch.setattr("app.notifications.httpx.post", failing_post)
    assert notifications.fire_webhook("charge-complete", "t", "b") is False   # never raises


def test_notify_fires_webhook_independently_of_push(session, monkeypatch):
    """The event webhook must fire even when push isn't configured at all —
    the two delivery channels are independent."""
    monkeypatch.setattr(
        "app.notifications.get_settings",
        lambda: _settings(event_webhook_url="https://example.invalid/hook"),  # no VAPID keys
    )
    sent = {}

    def fake_post(url, json=None, timeout=None):
        sent["url"], sent["json"] = url, json
        return httpx.Response(200, request=httpx.Request("POST", url))

    monkeypatch.setattr("app.notifications.httpx.post", fake_post)
    # No subscribers and no VAPID -> push delivers to 0, but the webhook still fires.
    sent_count = notifications.notify(session, "Battery low", "20%", tag="low-soc")
    assert sent_count == 0
    assert sent["json"]["event"] == "low-soc"
    assert sent["json"]["title"] == "Battery low"


def test_telegram_is_skipped_until_both_halves_are_configured(monkeypatch):
    """A half-configured messenger must not post to a malformed URL.

    A token without a chat id would build .../sendMessage with no recipient —
    which fails at Telegram rather than here, silently, on the one message
    that mattered.
    """
    calls = []
    monkeypatch.setattr("app.notifications.httpx.post",
                        lambda *a, **k: calls.append(a) or httpx.Response(
                            200, request=httpx.Request("POST", "https://x")))

    monkeypatch.setattr("app.notifications.get_settings",
                        lambda: _settings(telegram_bot_token="abc"))
    assert notifications.send_telegram("t", "b") is False
    monkeypatch.setattr("app.notifications.get_settings",
                        lambda: _settings(telegram_chat_id="123"))
    assert notifications.send_telegram("t", "b") is False
    assert calls == []

    monkeypatch.setattr("app.notifications.get_settings",
                        lambda: _settings(telegram_bot_token="abc",
                                          telegram_chat_id="123"))
    assert notifications.send_telegram("Sentry alert", "movement") is True
    assert "api.telegram.org/botabc/sendMessage" in calls[0][0]


def test_whatsapp_runs_beside_telegram_not_instead_of_it(monkeypatch):
    """Both channels fire, so the unofficial one going quiet costs nothing.

    CallMeBot is one person's free relay; Telegram is a first-party API. An
    alarm should not depend on the former, but there is no reason not to have
    both.
    """
    hit = []
    monkeypatch.setattr("app.notifications.httpx.post",
                        lambda url, **k: hit.append(("post", url)) or httpx.Response(
                            200, request=httpx.Request("POST", url)))
    monkeypatch.setattr("app.notifications.httpx.get",
                        lambda url, **k: hit.append(("get", url)) or httpx.Response(
                            200, request=httpx.Request("GET", url)))
    monkeypatch.setattr("app.notifications.get_settings",
                        lambda: _settings(telegram_bot_token="abc",
                                          telegram_chat_id="123",
                                          whatsapp_phone="+60123456789",
                                          whatsapp_apikey="key"))
    assert notifications.send_telegram("Sentry alert", "movement") is True
    assert notifications.send_whatsapp("Sentry alert", "movement") is True
    assert [k for k, _ in hit] == ["post", "get"]
    assert "callmebot.com/whatsapp.php" in hit[1][1]


def test_whatsapp_is_skipped_until_both_halves_are_configured(monkeypatch):
    """A phone number without a key would post to CallMeBot and be refused."""
    calls = []
    monkeypatch.setattr("app.notifications.httpx.get",
                        lambda url, **k: calls.append(url) or httpx.Response(
                            200, request=httpx.Request("GET", url)))
    monkeypatch.setattr("app.notifications.get_settings",
                        lambda: _settings(whatsapp_phone="+60123456789"))
    assert notifications.send_whatsapp("t", "b") is False
    assert calls == []


def test_ntfy_marks_a_sentry_alert_urgent(monkeypatch):
    """An alarm should break through a silenced phone; a charge should not."""
    sent = []

    def fake_post(url, **kw):
        sent.append((url, kw.get("headers", {})))
        return httpx.Response(200, request=httpx.Request("POST", url))

    monkeypatch.setattr("app.notifications.httpx.post", fake_post)
    monkeypatch.setattr("app.notifications.get_settings",
                        lambda: _settings(ntfy_topic="a-long-random-topic"))

    assert notifications.send_ntfy("Sentry alert", "movement", urgent=True) is True
    assert notifications.send_ntfy("Charge complete", "80%") is True
    assert sent[0][0].endswith("/a-long-random-topic")
    assert sent[0][1]["Priority"] == "urgent"
    assert sent[1][1]["Priority"] == "default"


def test_ntfy_is_skipped_without_a_topic(monkeypatch):
    calls = []
    monkeypatch.setattr("app.notifications.httpx.post",
                        lambda url, **k: calls.append(url))
    monkeypatch.setattr("app.notifications.get_settings", lambda: _settings())
    assert notifications.send_ntfy("t", "b") is False
    assert calls == []
