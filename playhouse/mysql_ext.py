import json

try:
    import mysql.connector as mysql_connector
except ImportError:
    mysql_connector = None
try:
    import mariadb
except ImportError:
    mariadb = None

from peewee import ImproperlyConfigured
from peewee import Insert
from peewee import MySQLDatabase
from peewee import NodeList
from peewee import SQL
from peewee import TextField
from peewee import fn


class MySQLConnectorDatabase(MySQLDatabase):
    def _connect(self):
        if mysql_connector is None:
            raise ImproperlyConfigured('MySQL connector not installed!')
        return mysql_connector.connect(db=self.database, autocommit=True,
                                       **self.connect_params)

    def cursor(self, commit=None):
        if self.is_closed():
            if self.autoconnect:
                self.connect()
            else:
                raise InterfaceError('Error, database connection not opened.')
        return self._state.conn.cursor(buffered=True)


class MariaDBConnectorDatabase(MySQLDatabase):
    def _connect(self):
        if mariadb is None:
            raise ImproperlyConfigured('mariadb connector not installed!')
        self.connect_params.pop('charset', None)
        self.connect_params.pop('sql_mode', None)
        self.connect_params.pop('use_unicode', None)
        return mariadb.connect(db=self.database, autocommit=True,
                               **self.connect_params)

    def cursor(self, commit=None):
        if self.is_closed():
            if self.autoconnect:
                self.connect()
            else:
                raise InterfaceError('Error, database connection not opened.')
        return self._state.conn.cursor(buffered=True)

    def _set_server_version(self, conn):
        version = conn.server_version
        version, point = divmod(version, 100)
        version, minor = divmod(version, 100)
        self.server_version = (version, minor, point)
        if self.server_version >= (10, 5, 0):
            self.returning_clause = True

    def last_insert_id(self, cursor, query_type=None):
        if not self.returning_clause:
            return cursor.lastrowid
        elif query_type == Insert.SIMPLE:
            try:
                return cursor[0][0]
            except (AttributeError, IndexError):
                return cursor.lastrowid
        return cursor


class JSONField(TextField):
    field_type = 'JSON'

    def db_value(self, value):
        if value is not None:
            return json.dumps(value)

    def python_value(self, value):
        if value is not None:
            return json.loads(value)


def Match(columns, expr, modifier=None):
    if isinstance(columns, (list, tuple)):
        match = fn.MATCH(*columns)  # Tuple of one or more columns / fields.
    else:
        match = fn.MATCH(columns)  # Single column / field.
    args = expr if modifier is None else NodeList((expr, SQL(modifier)))
    return NodeList((match, fn.AGAINST(args)))
