"""Payment capture with retries."""

import time

MAX_ATTEMPTS = 3


class PaymentError(Exception):
    """The payment provider rejected or failed a charge."""


def charge_with_retry(gateway, amount, attempts=MAX_ATTEMPTS):
    """Charge a payment gateway, retrying transient failures with backoff."""
    for attempt in range(1, attempts + 1):
        try:
            return gateway.charge(amount)
        except PaymentError:
            if attempt == attempts:
                raise
            time.sleep(0.1 * attempt)
    return None
