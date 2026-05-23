import unittest

from ledger import FakeGateway, GatewayTimeout, PaymentLedger


class PaymentLedgerTests(unittest.TestCase):
    def test_retry_with_same_key_does_not_create_second_remote_charge(self):
        gateway = FakeGateway()
        ledger = PaymentLedger(gateway)

        with self.assertRaises(GatewayTimeout):
            ledger.charge("cust_123", 5000, "order-1")

        charge = ledger.charge("cust_123", 5000, "order-1")

        self.assertEqual(charge["provider_id"], "ch_1")
        self.assertEqual(len(gateway.remote_charges), 1)
        self.assertEqual(len(ledger.local_charges), 1)


if __name__ == "__main__":
    unittest.main()
