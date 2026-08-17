import json
import sys

from ledger import FakeGateway, PaymentLedger


request = json.load(sys.stdin)
gateway = FakeGateway()
ledger = PaymentLedger(gateway)
outcomes = []

for operation in request["operations"]:
    try:
        if operation["target"] == "gateway":
            result = gateway.charge(
                operation["amount_cents"], operation["idempotency_key"]
            )
        elif operation["target"] == "ledger":
            result = ledger.charge(
                operation["customer_id"],
                operation["amount_cents"],
                operation["idempotency_key"],
            )
        else:
            raise ValueError("unsupported operation target")
        outcomes.append({"result": result})
    except Exception as exc:
        outcomes.append({"error": type(exc).__name__})

print(
    json.dumps(
        {
            "schema_version": 1,
            "observation": {
                "outcomes": outcomes,
                "remote_charges": gateway.remote_charges,
                "local_charges": ledger.local_charges,
            },
        },
        sort_keys=True,
    )
)
