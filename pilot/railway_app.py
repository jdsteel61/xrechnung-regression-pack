"""Railway staging entrypoint.

Serves the existing pilot HTTP surface on 0.0.0.0:$PORT, backed by the Railway
PostgreSQL database through the restricted `xreg_api` role, with an
unauthenticated `/health` readiness probe. Payments use the offline durable
provider until a real Stripe TEST sandbox key/webhook secret is supplied.

The validation worker is NOT started here: the pinned validator requires the
Windows JRE and Windows process containment (see pilot/process.py,
pilot/worker.py) and cannot run on Railway's Linux containers.
"""
import os
import secrets
from pathlib import Path

from . import launch_config
from .payments import DurableOfflineStripe, EraRoutingStripe, PaymentError, StripeSandbox
from .object_storage import S3RawFixtures
from .postgres_state import PostgresState
from .server import check_operator_tokens, make_server
from .service import Service


def public_origin():
    domain = os.environ.get("PUBLIC_ORIGIN") or os.environ.get("RAILWAY_PUBLIC_DOMAIN")
    if not domain:
        return None
    return domain if domain.startswith("https://") else "https://" + domain


def build_provider(state, launch=launch_config.SAFE_DEFAULT):
    # Live Stripe only through a validated LaunchConfig; otherwise the real
    # Stripe TEST sandbox when a test key + public https origin are present;
    # the durable offline simulator only on explicit opt-in. The adapter refuses
    # a key whose mode does not match, and any non-https origin.
    origin = public_origin()
    if launch.live_payments:
        if not origin:
            raise PaymentError("public_origin_required")
        primary = StripeSandbox(os.environ["STRIPE_LIVE_KEY"], origin, live=True)
        # TEST-era orders already in the database (cs_test_/pi_test_ ids) keep
        # reconciling through a restricted TEST-key legacy client while the
        # service runs with the live key; EraRoutingStripe routes ids strictly
        # by era prefix so one era's credential never sees the other's objects.
        test_key = os.environ.get("STRIPE_TEST_KEY", "")
        if test_key.startswith("sk_test_"):
            return EraRoutingStripe(primary, StripeSandbox(test_key, origin))
        return primary
    key = os.environ.get("STRIPE_TEST_KEY", "")
    if key.startswith("sk_test_") and origin:
        return StripeSandbox(key, origin)
    # The simulator exposes an unauthenticated /simulate-payment route. A public
    # deployment with a missing key must fail to start, never grant free entitlement.
    if os.environ.get("XREG_ALLOW_SIMULATED_PAYMENTS") != "1":
        raise PaymentError("stripe_test_key_and_public_origin_required")
    return DurableOfflineStripe(state / "provider.sqlite3")


def build_service():
    # Fail closed: malformed or incomplete enablement config stops startup.
    launch = launch_config.load(os.environ)
    state = Path(os.environ.get("XREG_STATE", "/tmp/xreg-state"))
    state.mkdir(parents=True, exist_ok=True)
    database = PostgresState(
        host=os.environ["PGHOST"],
        port=int(os.environ["PGPORT"]),
        dbname=os.environ["PGDATABASE"],
        user=os.environ["PGUSER"],
        password=os.environ["PGPASSWORD"],
    )
    provider = build_provider(state, launch)
    # A stable secret survives restarts so in-flight webhook signatures remain
    # verifiable; a fresh random value is only a fallback for the first boot.
    # With the real sandbox this MUST be the Stripe webhook endpoint's secret.
    # In live mode both endpoint secrets verify during the cutover window:
    # live events with STRIPE_LIVE_WEBHOOK_SECRET and TEST-era events still
    # in flight with the previous WEBHOOK_SECRET, when it is set.
    if launch.live_payments:
        secret = (os.environ["STRIPE_LIVE_WEBHOOK_SECRET"],)
        if os.environ.get("WEBHOOK_SECRET"):
            secret += (os.environ["WEBHOOK_SECRET"],)
    else:
        secret = os.environ.get("WEBHOOK_SECRET") or secrets.token_urlsafe(32)
    raw_store = None
    bucket = os.environ.get("S3_RAW_BUCKET")
    if bucket:
        import boto3
        raw_store = S3RawFixtures(boto3.client("s3", endpoint_url=os.environ.get("S3_ENDPOINT")), bucket)
    return Service(state, provider, secret, database=database, raw_store=raw_store, launch=launch)


def operator_tokens(env):
    """Split credentials; the legacy single token is refused, not reused."""
    if env.get("OPERATOR_TOKEN"):
        raise ValueError("legacy_operator_token_set_use_split_tokens")
    read, action = env.get("OPERATOR_READ_TOKEN") or None, env.get("OPERATOR_ACTION_TOKEN") or None
    check_operator_tokens(read, action)
    return read, action


def start_watchdog(env=None, read_token=None, action_token=None):
    """Grok-bot layer 1 (pilot/watchdog.py): env-gated background sweep loop.

    OFF unless WATCHDOG_ENABLED=true. Probes the PUBLIC origin (not the local
    server) so edge/DNS failures are caught too, alerts by email when the
    owner has set WATCHDOG_SMTP_* secrets, and otherwise logs findings to the
    container logs via the console sink. The loop is read-only against the
    bounded operator API and can never touch money or gates.
    """
    import threading
    import time as _time

    from . import watchdog as _watchdog

    env = os.environ if env is None else env
    if env.get("WATCHDOG_ENABLED") != "true":
        return None
    origin = env.get("WATCHDOG_ORIGIN") or public_origin()
    if not origin or not (read_token and action_token):
        raise ValueError("watchdog_requires_origin_and_operator_tokens")
    interval = max(60, int(env.get("WATCHDOG_INTERVAL_SECONDS", "900")))
    state_path = Path(os.environ.get("XREG_STATE", "/tmp/xreg-state")) / "watchdog-state.json"
    try:
        sink = _watchdog.SmtpSink.from_env(env)
    except ValueError:
        sink = _watchdog.ConsoleSink()  # findings land in container logs

    def loop():
        while True:
            try:
                state = _watchdog.load_state(state_path)
                _watchdog.run_once(origin, read_token, action_token, state, sink)
                _watchdog.save_state(state_path, state)
            except Exception as exc:  # the loop must never take the app down
                print(f"[watchdog] sweep failed: {exc!r}", flush=True)
            _time.sleep(interval)

    thread = threading.Thread(target=loop, name="xreg-watchdog", daemon=True)
    thread.start()
    return thread


def main():
    port = int(os.environ.get("PORT", "8080"))
    read_token, action_token = operator_tokens(os.environ)
    service = build_service()
    start_watchdog(read_token=read_token, action_token=action_token)
    server = make_server(service, port, deploy=True,
                         operator_token=action_token, operator_read_token=read_token)
    print(f"xreg staging API listening on 0.0.0.0:{port}", flush=True)
    try:
        server.serve_forever()
    finally:
        server.server_close()
        service.close()


if __name__ == "__main__":
    main()
