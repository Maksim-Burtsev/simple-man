import json
import sys

from ledger import FakeGateway, GatewayTimeout, PaymentLedger


request = json.load(sys.stdin)
scenario = request["scenario"]
errors = []

if scenario == "timeout_retry_repeat":
    gateway = FakeGateway()
    ledger = PaymentLedger(gateway)
    try:
        ledger.charge("cust_123", 5000, "order-1")
    except GatewayTimeout:
        errors.append("GatewayTimeout")
    first = ledger.charge("cust_123", 5000, "order-1")
    replay = ledger.charge("cust_123", 5000, "order-1")
    observation = {
        "errors": errors,
        "replay_equal": first == replay,
        "remote_count": len(gateway.remote_charges),
        "local_count": len(ledger.local_charges),
        "remote_keys": [charge["idempotency_key"] for charge in gateway.remote_charges],
        "local_keys": [charge["idempotency_key"] for charge in ledger.local_charges],
        "remote_amounts": [charge["amount_cents"] for charge in gateway.remote_charges],
        "local_amounts": [charge["amount_cents"] for charge in ledger.local_charges],
        "local_customers": [charge["customer_id"] for charge in ledger.local_charges],
        "provider_mapping_valid": [
            charge["provider_id"] for charge in ledger.local_charges
        ]
        == [charge["id"] for charge in gateway.remote_charges],
    }
elif scenario == "independent_second_key":
    gateway = FakeGateway()
    ledger = PaymentLedger(gateway)
    try:
        ledger.charge("cust_123", 5000, "order-1")
    except GatewayTimeout:
        errors.append("GatewayTimeout")
    first = ledger.charge("cust_123", 5000, "order-1")
    second = ledger.charge("cust_123", 5000, "order-2")
    observation = {
        "errors": errors,
        "distinct_charge": first["provider_id"] != second["provider_id"],
        "remote_count": len(gateway.remote_charges),
        "local_count": len(ledger.local_charges),
        "remote_keys": [charge["idempotency_key"] for charge in gateway.remote_charges],
        "local_keys": [charge["idempotency_key"] for charge in ledger.local_charges],
        "remote_amounts": [charge["amount_cents"] for charge in gateway.remote_charges],
        "local_amounts": [charge["amount_cents"] for charge in ledger.local_charges],
        "local_customers": [charge["customer_id"] for charge in ledger.local_charges],
        "provider_mapping_valid": [
            charge["provider_id"] for charge in ledger.local_charges
        ]
        == [charge["id"] for charge in gateway.remote_charges],
    }
elif scenario == "replay_old_after_new":
    gateway = FakeGateway()
    try:
        gateway.charge(5000, "order-1")
    except GatewayTimeout:
        errors.append("GatewayTimeout")
    first = gateway.charge(5000, "order-1")
    second = gateway.charge(5000, "order-2")
    replay = gateway.charge(5000, "order-1")
    observation = {
        "errors": errors,
        "replay_equal": first == replay,
        "distinct_second": first["id"] != second["id"],
        "remote_count": len(gateway.remote_charges),
        "remote_keys": [charge["idempotency_key"] for charge in gateway.remote_charges],
        "remote_amounts": [charge["amount_cents"] for charge in gateway.remote_charges],
    }
else:
    raise ValueError("unknown scenario")

print(
    json.dumps(
        {
            "schema_version": 1,
            "case_id": request["case_id"],
            "observation": observation,
        },
        sort_keys=True,
    )
)
