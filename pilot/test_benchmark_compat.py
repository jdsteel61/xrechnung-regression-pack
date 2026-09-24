"""A2-06 regression: the benchmark must not call dict(row) on storage rows.

PostgresState.Row.__iter__ yields values only, so dict(row) raises on the
Postgres adapter while it happens to work on sqlite3.Row. The benchmark's row
consumption goes through benchmark._row_dict, which must produce the same
mapping for both adapters. This module is part of the regression pack only;
it exercises no served path and no network.
"""
import inspect
import re
import sqlite3
import unittest

from . import benchmark
from .benchmark import _row_dict
from .postgres_state import Row


class BenchmarkRowAccess(unittest.TestCase):
    """benchmark._row_dict is adapter-safe (sqlite3.Row and PostgresState.Row)."""

    NAMES = ('at', 'kind', 'order_id', 'data')
    VALUES = ('2026-09-21T10:00:00Z', 'scope_viewed', 'ord_test', '{"n":1}')

    def _sqlite_row(self):
        connection = sqlite3.connect(':memory:')
        connection.row_factory = sqlite3.Row
        try:
            return connection.execute(
                'SELECT ? AS at, ? AS kind, ? AS order_id, ? AS data',
                self.VALUES).fetchone()
        finally:
            connection.close()

    def test_row_dict_adapter_safe(self):
        expected = dict(zip(self.NAMES, self.VALUES))
        for row in (self._sqlite_row(), Row(list(self.NAMES), self.VALUES)):
            self.assertEqual(_row_dict(row), expected)
            self.assertEqual(list(_row_dict(row)), list(self.NAMES))

    def test_row_dict_shape_hazard_documented(self):
        # The adapter hazard stays as documented: dict(Row) must still raise.
        row = Row(list(self.NAMES), self.VALUES)
        with self.assertRaises((TypeError, ValueError)):
            dict(row)

    def test_benchmark_avoids_dict_on_rows(self):
        source = inspect.getsource(benchmark)
        # No bare dict(...) call on rows (word-boundary guard keeps _row_dict legal).
        self.assertIsNone(re.search(r'(?<![\w.])dict\(', source))
        self.assertIn("_row_dict(row) for row in service.db.execute", source)


if __name__ == '__main__':
    unittest.main()
