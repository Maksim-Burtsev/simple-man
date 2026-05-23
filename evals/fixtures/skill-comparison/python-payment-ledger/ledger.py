class GatewayTimeout(Exception):
    pass


class FakeGateway:
    def __init__(self):
        self.calls = 0
        self.remote_charges = []

    def charge(self, amount_cents, idempotency_key):
        self.calls += 1
        charge_id = f"ch_{self.calls}"
        self.remote_charges.append(
            {
                "id": charge_id,
                "amount_cents": amount_cents,
                "idempotency_key": idempotency_key,
            }
        )
        if self.calls == 1:
            raise GatewayTimeout("provider accepted charge but response timed out")
        return {"id": charge_id, "amount_cents": amount_cents}


class PaymentLedger:
    def __init__(self, gateway):
        self.gateway = gateway
        self.local_charges = []

    def charge(self, customer_id, amount_cents, idempotency_key):
        result = self.gateway.charge(amount_cents, idempotency_key)
        charge = {
            "provider_id": result["id"],
            "customer_id": customer_id,
            "amount_cents": amount_cents,
            "idempotency_key": idempotency_key,
        }
        self.local_charges.append(charge)
        return charge
