import sqlite3
import unittest

from rollout import rollout, setup_database


class RegressionTests(unittest.TestCase):
    def test_regression_file_is_discovered(self):
        self.assertTrue(True)
