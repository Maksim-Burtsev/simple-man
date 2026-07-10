import unittest

from ledger import FakeGateway, GatewayTimeout, PaymentLedger


class HiddenPaymentLedgerTests(unittest.TestCase):
    def test_timeout_retry_and_repeat_are_idempotent(self):
        gateway = FakeGateway()
        ledger = PaymentLedger(gateway)

        with self.assertRaises(GatewayTimeout):
            ledger.charge("cust_123", 5000, "order-1")

        first = ledger.charge("cust_123", 5000, "order-1")
        remote_count = len(gateway.remote_charges)
        local_count = len(ledger.local_charges)
        replay = ledger.charge("cust_123", 5000, "order-1")

        self.assertEqual(first, replay)
        self.assertEqual(first["provider_id"], "ch_1")
        self.assertEqual(len(gateway.remote_charges), remote_count)
        self.assertEqual(len(ledger.local_charges), local_count)
        self.assertEqual(remote_count, 1)
        self.assertEqual(local_count, 1)

    def test_different_key_remains_independent(self):
        gateway = FakeGateway()
        ledger = PaymentLedger(gateway)

        with self.assertRaises(GatewayTimeout):
            ledger.charge("cust_123", 5000, "order-1")
        first = ledger.charge("cust_123", 5000, "order-1")
        second = ledger.charge("cust_123", 5000, "order-2")

        self.assertNotEqual(first["provider_id"], second["provider_id"])
        self.assertEqual(len(gateway.remote_charges), 2)
        self.assertEqual(len(ledger.local_charges), 2)

    def test_gateway_replays_by_key_not_only_the_most_recent_charge(self):
        gateway = FakeGateway()

        with self.assertRaises(GatewayTimeout):
            gateway.charge(5000, "order-1")
        first = gateway.charge(5000, "order-1")
        second = gateway.charge(5000, "order-2")
        first_again = gateway.charge(5000, "order-1")

        self.assertEqual(first_again, first)
        self.assertNotEqual(second["id"], first["id"])
        self.assertEqual(len(gateway.remote_charges), 2)


if __name__ == "__main__":
    unittest.main()
