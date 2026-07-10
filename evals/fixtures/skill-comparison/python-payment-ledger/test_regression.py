import unittest

from ledger import FakeGateway, GatewayTimeout, PaymentLedger


class RegressionTests(unittest.TestCase):
    def test_regression_file_is_discovered(self):
        self.assertTrue(True)
