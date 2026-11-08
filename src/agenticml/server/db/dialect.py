"""SQL variations kept outside repositories (Postgres follows in package 19)."""


class SqliteDialect:
    def placeholders(self, sql):
        return sql

    def migration(self, sql):
        for token, value in {"id": "INTEGER PRIMARY KEY", "blob": "BLOB", "real": "REAL"}.items():
            sql = sql.replace("{{" + token + "}}", value)
        return sql

    def lock_row(self, tx, table, where_sql, params=()):
        # BEGIN IMMEDIATE already holds the database write lock.
        pass

    def advisory_lock(self, tx, name):
        pass

    def upsert(self, table, columns, conflict, updates):
        names = ', '.join(columns)
        values = ', '.join('?' for _ in columns)
        action = ('DO UPDATE SET ' + ', '.join(f'{c}=excluded.{c}' for c in updates)
                  if updates else 'DO NOTHING')
        return (f'INSERT INTO {table} ({names}) VALUES ({values}) '
                f'ON CONFLICT ({", ".join(conflict)}) {action}')
