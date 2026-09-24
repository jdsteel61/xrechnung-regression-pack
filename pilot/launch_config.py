"""Fail-closed launch configuration.

Customer uploads and live payments are enabled only by explicit environment
configuration that validates at startup. Absent variables mean the reviewed
safe default (both off). Any malformed value, or an enablement without its
required acknowledgement/credentials, raises and prevents the process starting.
No source edit is needed, or possible, to enable the reviewed production path.
"""
from dataclasses import dataclass

# Changing this string is a source edit and therefore a new review. Setting it
# in the environment records that the operator enabled THIS reviewed path.
PRODUCTION_ACK = 'reviewed-production-path-v1'
TAX_TREATMENTS = ('no_vat_charged_seller_not_registered',)


class LaunchConfigError(ValueError):
    pass


@dataclass(frozen=True)
class LaunchConfig:
    live_payments: bool = False
    customer_uploads: bool = False
    tax_treatment: str | None = None

    @property
    def livemode(self):
        return self.live_payments


SAFE_DEFAULT = LaunchConfig()


def _flag(env, name):
    value = env.get(name)
    if value is None or value == '':
        return False
    if value == 'true':
        return True
    if value == 'false':
        return False
    raise LaunchConfigError(name.lower()+'_must_be_true_or_false')


def load(env):
    live = _flag(env, 'XREG_LIVE_PAYMENTS')
    uploads = _flag(env, 'XREG_CUSTOMER_UPLOADS')
    if (live or uploads) and env.get('XREG_PRODUCTION_ACK') != PRODUCTION_ACK:
        raise LaunchConfigError('production_enablement_requires_explicit_ack')
    tax = None
    if live:
        if not env.get('STRIPE_LIVE_KEY', '').startswith('sk_live_'):
            raise LaunchConfigError('live_payments_require_stripe_live_key')
        if not env.get('STRIPE_LIVE_WEBHOOK_SECRET', '').startswith('whsec_'):
            raise LaunchConfigError('live_payments_require_live_webhook_secret')
        tax = env.get('XREG_TAX_TREATMENT')
        if tax not in TAX_TREATMENTS:
            raise LaunchConfigError('live_payments_require_declared_tax_treatment')
        if env.get('XREG_ALLOW_SIMULATED_PAYMENTS') == '1':
            raise LaunchConfigError('simulated_payments_forbidden_with_live_payments')
    return LaunchConfig(live_payments=live, customer_uploads=uploads, tax_treatment=tax)
