import json
import subprocess
import sys
import unittest


class PaymentLedgerTests(unittest.TestCase):
    def test_retry_with_same_key_does_not_create_second_remote_charge(self):
        request = {
            "operations": [
                {
                    "target": "ledger",
                    "customer_id": "cust_123",
                    "amount_cents": 5000,
                    "idempotency_key": "order-1",
                },
                {
                    "target": "ledger",
                    "customer_id": "cust_123",
                    "amount_cents": 5000,
                    "idempotency_key": "order-1",
                },
            ]
        }
        completed = subprocess.run(
            (sys.executable, "app.py"),
            input=json.dumps(request) + "\n",
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stderr, "")
        observation = json.loads(completed.stdout)["observation"]

        self.assertEqual(
            observation["outcomes"],
            [
                {"error": "GatewayTimeout"},
                {
                    "result": {
                        "provider_id": "ch_1",
                        "customer_id": "cust_123",
                        "amount_cents": 5000,
                        "idempotency_key": "order-1",
                    }
                },
            ],
        )
        self.assertEqual(len(observation["remote_charges"]), 1)
        self.assertEqual(len(observation["local_charges"]), 1)

    def test_gateway_target_uses_same_operation_schema(self):
        request = {
            "operations": [
                {
                    "target": "gateway",
                    "customer_id": "cust_123",
                    "amount_cents": 5000,
                    "idempotency_key": "order-1",
                }
            ]
        }
        completed = subprocess.run(
            (sys.executable, "app.py"),
            input=json.dumps(request) + "\n",
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stderr, "")
        observation = json.loads(completed.stdout)["observation"]

        self.assertEqual(observation["outcomes"], [{"error": "GatewayTimeout"}])
        self.assertEqual(len(observation["remote_charges"]), 1)
        self.assertEqual(observation["local_charges"], [])


if __name__ == "__main__":
    unittest.main()
