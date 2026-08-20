"""Stripe adapter. Adapter #2, and the one that stresses the design.

Two things break here that GitHub never exercises:

  1. Spec size. Stripe's OpenAPI document is an order of magnitude larger than
     GitHub's, so retrieval quality stops being academic.
  2. Encoding. Stripe's v1 API takes application/x-www-form-urlencoded with
     bracket notation for nested fields (metadata[order_id]), not JSON. The
     executor currently posts JSON, so flatten the body here before writing
     Connect write-tasks, or they will all fail for the wrong reason.

Connect note for the eval set: the v1 Accounts API and the v2 Accounts API
model connected accounts differently (v2 uses a single Account carrying
merchant / customer / recipient configurations). Each task should say which
it targets, and the API version header should be pinned per task.
"""

from __future__ import annotations

from .base import Adapter


class StripeAdapter(Adapter):
    name = "stripe"
    spec_url = "https://raw.githubusercontent.com/stripe/openapi/master/openapi/spec3.json"
    base_url = "https://api.stripe.com"
    auth_env = "STRIPE_SECRET_KEY"

    def headers(self) -> dict:
        key = self.require_auth()
        if not key.startswith("sk_test_"):
            raise SystemExit(
                "Refusing to run: STRIPE_SECRET_KEY is not a test-mode key (sk_test_...)."
            )
        return {
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/x-www-form-urlencoded",
        }
