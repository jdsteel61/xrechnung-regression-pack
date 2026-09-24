"""PostgreSQL storage adapter for the pilot's existing transactional SQL calls.

The Railway migration must be applied before this adapter is constructed.
This does not provide tenant isolation or object storage.
"""
import re

import psycopg


TABLES = ('orders', 'events', 'webhooks', 'gates', 'reports',
          'maintenance', 'submissions', 'checkouts', 'gate_audit', 'checkout_rate')


class Row:
    def __init__(self, names, values):
        self.names, self.values = {name: i for i, name in enumerate(names)}, values

    def __getitem__(self, key):
        return self.values[self.names[key] if isinstance(key, str) else key]

    def __iter__(self):
        return iter(self.values)


class Cursor:
    def __init__(self, cursor):
        self.cursor = cursor
        self.rowcount = cursor.rowcount

    def _row(self, values):
        return None if values is None else Row(
            [column.name for column in self.cursor.description], values)

    def fetchone(self):
        return self._row(self.cursor.fetchone())

    def fetchall(self):
        return [self._row(values) for values in self.cursor.fetchall()]

    def __iter__(self):
        for values in self.cursor:
            yield self._row(values)


class PostgresState:
    def __init__(self, *, host, port, dbname, user, password):
        self.connect_args = dict(host=host, port=port, dbname=dbname, user=user,
                                 password=password, connect_timeout=10, autocommit=True)
        self.connection = psycopg.connect(
            **self.connect_args,
        )
        self.transactions = []
        table = self.connection.execute(
            "SELECT to_regclass('xreg.orders') IS NOT NULL"
        ).fetchone()[0]
        if not table:
            self.connection.close()
            raise ValueError('postgres_migration_required')

    def claim_job(self):
        """Hold one cluster-wide execution lease until this connection closes."""
        return self._claim_lock(781964120517)

    def claim_maintenance(self):
        """Serialize maintenance and identify work abandoned by a dead process."""
        return self._claim_lock(781964120518)

    def _claim_lock(self, key):
        lease = psycopg.connect(**self.connect_args)
        try:
            if not lease.execute('SELECT pg_try_advisory_lock(%s)', (key,)).fetchone()[0]:
                lease.close()
                return None
            return lease
        except Exception:
            lease.close()
            raise

    def executescript(self, script):
        # The SQLite bootstrap script is replaced by an explicit migration.
        if 'CREATE TABLE IF NOT EXISTS orders' not in script:
            raise ValueError('unexpected_sqlite_bootstrap')

    def execute(self, sql, params=()):
        if sql.startswith('INSERT OR REPLACE INTO orders VALUES'):
            sql = ('INSERT INTO orders VALUES (?,?) ON CONFLICT (id) '
                   'DO UPDATE SET data=EXCLUDED.data')
        elif sql.startswith('INSERT OR REPLACE INTO reports VALUES'):
            sql = ('INSERT INTO reports VALUES (?,?,?,?,?) ON CONFLICT (order_id,run,name) '
                   'DO UPDATE SET content=EXCLUDED.content, expires=EXCLUDED.expires')
        elif sql.startswith('INSERT OR IGNORE INTO gates VALUES'):
            sql = 'INSERT INTO gates VALUES (?,1) ON CONFLICT (name) DO NOTHING'
        sql = sql.replace('ORDER BY rowid DESC', 'ORDER BY expires DESC, run DESC')
        sql = re.sub(r'\b(' + '|'.join(TABLES) + r')\b',
                     lambda match: 'xreg.' + match.group(1), sql)
        sql = sql.replace('?', '%s')
        return Cursor(self.connection.execute(sql, params))

    def commit(self):
        if self.transactions:
            raise ValueError('commit_inside_transaction')

    def __enter__(self):
        transaction = self.connection.transaction()
        transaction.__enter__()
        self.transactions.append(transaction)
        return self

    def __exit__(self, kind, value, traceback):
        transaction = self.transactions.pop()
        return transaction.__exit__(kind, value, traceback)

    def close(self):
        self.connection.close()
