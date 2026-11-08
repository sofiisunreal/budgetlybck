from billing.services.access import apply_verification
from billing.services.paystack import verify_transaction


def verify_and_activate_purchase(reference):
    return apply_verification(reference, verify_transaction(reference))
