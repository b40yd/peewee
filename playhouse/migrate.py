"""
Lightweight schema migrations.

Example Usage
-------------

Instantiate a migrator:

    # Postgres example:
    my_db = PostgresqlDatabase(...)
    migrator = PostgresqlMigrator(my_db)

    # SQLite example:
    my_db = SqliteDatabase('my_database.db')
    migrator = SqliteMigrator(my_db)

Then you will use the `migrate` function to run various `Operation`s which
are generated by the migrator:

    migrate(
        migrator.add_column('some_table', 'column_name', CharField(default=''))
    )

Migrations are not run inside a transaction, so if you wish the migration to
run in a transaction you will need to wrap the call to `migrate` in a
transaction block, e.g.:

    with my_db.transaction():
        migrate(...)

Supported Operations
--------------------

Add new field(s) to an existing model:

    # Create your field instances. For non-null fields you must specify a
    # default value.
    pubdate_field = DateTimeField(null=True)
    comment_field = TextField(default='')

    # Run the migration, specifying the database table, field name and field.
    migrate(
        migrator.add_column('comment_tbl', 'pub_date', pubdate_field),
        migrator.add_column('comment_tbl', 'comment', comment_field),
    )

Renaming a field:

    # Specify the table, original name of the column, and its new name.
    migrate(
        migrator.rename_column('story', 'pub_date', 'publish_date'),
        migrator.rename_column('story', 'mod_date', 'modified_date'),
    )

Dropping a field:

    migrate(
        migrator.drop_column('story', 'some_old_field'),
    )

Making a field nullable or not nullable:

    # Note that when making a field not null that field must not have any
    # NULL values present.
    migrate(
        # Make `pub_date` allow NULL values.
        migrator.drop_not_null('story', 'pub_date'),

        # Prevent `modified_date` from containing NULL values.
        migrator.add_not_null('story', 'modified_date'),
    )

Renaming a table:

    migrate(
        migrator.rename_table('story', 'stories_tbl'),
    )

Adding an index:

    # Specify the table, column names, and whether the index should be
    # UNIQUE or not.
    migrate(
        # Create an index on the `pub_date` column.
        migrator.add_index('story', ('pub_date',), False),

        # Create a multi-column index on the `pub_date` and `status` fields.
        migrator.add_index('story', ('pub_date', 'status'), False),

        # Create a unique index on the category and title fields.
        migrator.add_index('story', ('category_id', 'title'), True),
    )

Dropping an index:

    # Specify the index name.
    migrate(migrator.drop_index('story', 'story_pub_date_status'))

Adding or dropping table constraints:

.. code-block:: python

    # Add a CHECK() constraint to enforce the price cannot be negative.
    migrate(migrator.add_constraint(
        'products',
        'price_check',
        Check('price >= 0')))

    # Remove the price check constraint.
    migrate(migrator.drop_constraint('products', 'price_check'))

    # Add a UNIQUE constraint on the first and last names.
    migrate(migrator.add_unique('person', 'first_name', 'last_name'))
"""
from collections import namedtuple
import functools
import hashlib
import re

from peewee import *
from peewee import CommaNodeList
from peewee import EnclosedNodeList
from peewee import Entity
from peewee import Expression
from peewee import Node
from peewee import NodeList
from peewee import OP
from peewee import callable_
from peewee import sort_models
from peewee import sqlite3
from peewee import _truncate_constraint_name
try:
    from playhouse.cockroachdb import CockroachDatabase
except ImportError:
    CockroachDatabase = None


class Operation(object):
    """Encapsulate a single schema altering operation."""
    def __init__(self, migrator, method, *args, **kwargs):
        self.migrator = migrator
        self.method = method
        self.args = args
        self.kwargs = kwargs

    def execute(self, node):
        self.migrator.database.execute(node)

    def _handle_result(self, result):
        if isinstance(result, (Node, Context)):
            self.execute(result)
        elif isinstance(result, Operation):
            result.run()
        elif isinstance(result, (list, tuple)):
            for item in result:
                self._handle_result(item)

    def run(self):
        kwargs = self.kwargs.copy()
        kwargs['with_context'] = True
        method = getattr(self.migrator, self.method)
        self._handle_result(method(*self.args, **kwargs))


def operation(fn):
    @functools.wraps(fn)
    def inner(self, *args, **kwargs):
        with_context = kwargs.pop('with_context', False)
        if with_context:
            return fn(self, *args, **kwargs)
        return Operation(self, fn.__name__, *args, **kwargs)
    return inner


def make_index_name(table_name, columns):
    index_name = '_'.join((table_name,) + tuple(columns))
    if len(index_name) > 64:
        index_hash = hashlib.md5(index_name.encode('utf-8')).hexdigest()
        index_name = '%s_%s' % (index_name[:56], index_hash[:7])
    return index_name


class SchemaMigrator(object):
    explicit_create_foreign_key = False
    explicit_delete_foreign_key = False

    def __init__(self, database):
        self.database = database

    def make_context(self):
        return self.database.get_sql_context()

    @classmethod
    def from_database(cls, database):
        if CockroachDatabase and isinstance(database, CockroachDatabase):
            return CockroachDBMigrator(database)
        elif isinstance(database, PostgresqlDatabase):
            return PostgresqlMigrator(database)
        elif isinstance(database, MySQLDatabase):
            return MySQLMigrator(database)
        elif isinstance(database, SqliteDatabase):
            return SqliteMigrator(database)
        raise ValueError('Unsupported database: %s' % database)

    @operation
    def apply_default(self, table, column_name, field):
        default = field.default
        if callable_(default):
            default = default()

        return (self.make_context()
                .literal('UPDATE ')
                .sql(Entity(table))
                .literal(' SET ')
                .sql(Expression(
                    Entity(column_name),
                    OP.EQ,
                    field.db_value(default),
                    flat=True)))

    def _alter_table(self, ctx, table):
        return ctx.literal('ALTER TABLE ').sql(Entity(table))

    def _alter_column(self, ctx, table, column):
        return (self
                ._alter_table(ctx, table)
                .literal(' ALTER COLUMN ')
                .sql(Entity(column)))

    @operation
    def alter_add_column(self, table, column_name, field):
        # Make field null at first.
        ctx = self.make_context()
        field_null, field.null = field.null, True

        # Set the field's column-name and name, if it is not set or doesn't
        # match the new value.
        if field.column_name != column_name:
            field.name = field.column_name = column_name

        (self
         ._alter_table(ctx, table)
         .literal(' ADD COLUMN ')
         .sql(field.ddl(ctx)))

        field.null = field_null
        if isinstance(field, ForeignKeyField):
            self.add_inline_fk_sql(ctx, field)
        return ctx

    @operation
    def add_constraint(self, table, name, constraint):
        return (self
                ._alter_table(self.make_context(), table)
                .literal(' ADD CONSTRAINT ')
                .sql(Entity(name))
                .literal(' ')
                .sql(constraint))

    @operation
    def add_unique(self, table, *column_names):
        constraint_name = 'uniq_%s' % '_'.join(column_names)
        constraint = NodeList((
            SQL('UNIQUE'),
            EnclosedNodeList([Entity(column) for column in column_names])))
        return self.add_constraint(table, constraint_name, constraint)

    @operation
    def drop_constraint(self, table, name):
        return (self
                ._alter_table(self.make_context(), table)
                .literal(' DROP CONSTRAINT ')
                .sql(Entity(name)))

    def add_inline_fk_sql(self, ctx, field):
        ctx = (ctx
               .literal(' REFERENCES ')
               .sql(Entity(field.rel_model._meta.table_name))
               .literal(' ')
               .sql(EnclosedNodeList((Entity(field.rel_field.column_name),))))
        if field.on_delete is not None:
            ctx = ctx.literal(' ON DELETE %s' % field.on_delete)
        if field.on_update is not None:
            ctx = ctx.literal(' ON UPDATE %s' % field.on_update)
        return ctx

    @operation
    def add_foreign_key_constraint(self, table, column_name, rel, rel_column,
                                   on_delete=None, on_update=None):
        constraint = 'fk_%s_%s_refs_%s' % (table, column_name, rel)
        ctx = (self
               .make_context()
               .literal('ALTER TABLE ')
               .sql(Entity(table))
               .literal(' ADD CONSTRAINT ')
               .sql(Entity(_truncate_constraint_name(constraint)))
               .literal(' FOREIGN KEY ')
               .sql(EnclosedNodeList((Entity(column_name),)))
               .literal(' REFERENCES ')
               .sql(Entity(rel))
               .literal(' (')
               .sql(Entity(rel_column))
               .literal(')'))
        if on_delete is not None:
            ctx = ctx.literal(' ON DELETE %s' % on_delete)
        if on_update is not None:
            ctx = ctx.literal(' ON UPDATE %s' % on_update)
        return ctx

    @operation
    def add_column(self, table, column_name, field):
        # Adding a column is complicated by the fact that if there are rows
        # present and the field is non-null, then we need to first add the
        # column as a nullable field, then set the value, then add a not null
        # constraint.
        if not field.null and field.default is None:
            raise ValueError('%s is not null but has no default' % column_name)

        is_foreign_key = isinstance(field, ForeignKeyField)
        if is_foreign_key and not field.rel_field:
            raise ValueError('Foreign keys must specify a `field`.')

        operations = [self.alter_add_column(table, column_name, field)]

        # In the event the field is *not* nullable, update with the default
        # value and set not null.
        if not field.null:
            operations.extend([
                self.apply_default(table, column_name, field),
                self.add_not_null(table, column_name)])

        if is_foreign_key and self.explicit_create_foreign_key:
            operations.append(
                self.add_foreign_key_constraint(
                    table,
                    column_name,
                    field.rel_model._meta.table_name,
                    field.rel_field.column_name,
                    field.on_delete,
                    field.on_update))

        if field.index or field.unique:
            using = getattr(field, 'index_type', None)
            operations.append(self.add_index(table, (column_name,),
                                             field.unique, using))

        return operations

    @operation
    def drop_foreign_key_constraint(self, table, column_name):
        raise NotImplementedError

    @operation
    def drop_column(self, table, column_name, cascade=True):
        ctx = self.make_context()
        (self._alter_table(ctx, table)
         .literal(' DROP COLUMN ')
         .sql(Entity(column_name)))

        if cascade:
            ctx.literal(' CASCADE')

        fk_columns = [
            foreign_key.column
            for foreign_key in self.database.get_foreign_keys(table)]
        if column_name in fk_columns and self.explicit_delete_foreign_key:
            return [self.drop_foreign_key_constraint(table, column_name), ctx]

        return ctx

    @operation
    def rename_column(self, table, old_name, new_name):
        return (self
                ._alter_table(self.make_context(), table)
                .literal(' RENAME COLUMN ')
                .sql(Entity(old_name))
                .literal(' TO ')
                .sql(Entity(new_name)))

    @operation
    def add_not_null(self, table, column):
        return (self
                ._alter_column(self.make_context(), table, column)
                .literal(' SET NOT NULL'))

    @operation
    def drop_not_null(self, table, column):
        return (self
                ._alter_column(self.make_context(), table, column)
                .literal(' DROP NOT NULL'))

    @operation
    def add_column_default(self, table, column, default):
        if default is None:
            raise ValueError('`default` must be not None/NULL.')
        if callable_(default):
            default = default()
        # Try to handle SQL functions and string literals, otherwise pass as a
        # bound value.
        if isinstance(default, str) and default.endswith((')', "'")):
            default = SQL(default)

        return (self
                ._alter_table(self.make_context(), table)
                .literal(' ALTER COLUMN ')
                .sql(Entity(column))
                .literal(' SET DEFAULT ')
                .sql(default))

    @operation
    def drop_column_default(self, table, column):
        return (self
                ._alter_table(self.make_context(), table)
                .literal(' ALTER COLUMN ')
                .sql(Entity(column))
                .literal(' DROP DEFAULT'))

    @operation
    def alter_column_type(self, table, column, field, cast=None):
        # ALTER TABLE <table> ALTER COLUMN <column>
        ctx = self.make_context()
        ctx = (self
               ._alter_column(ctx, table, column)
               .literal(' TYPE ')
               .sql(field.ddl_datatype(ctx)))
        if cast is not None:
            if not isinstance(cast, Node):
                cast = SQL(cast)
            ctx = ctx.literal(' USING ').sql(cast)
        return ctx

    @operation
    def rename_table(self, old_name, new_name):
        return (self
                ._alter_table(self.make_context(), old_name)
                .literal(' RENAME TO ')
                .sql(Entity(new_name)))

    @operation
    def add_index(self, table, columns, unique=False, using=None):
        ctx = self.make_context()
        index_name = make_index_name(table, columns)
        table_obj = Table(table)
        cols = [getattr(table_obj.c, column) for column in columns]
        index = Index(index_name, table_obj, cols, unique=unique, using=using)
        return ctx.sql(index)

    @operation
    def drop_index(self, table, index_name):
        return (self
                .make_context()
                .literal('DROP INDEX ')
                .sql(Entity(index_name)))


class PostgresqlMigrator(SchemaMigrator):
    def _primary_key_columns(self, tbl):
        query = """
            SELECT pg_attribute.attname
            FROM pg_index, pg_class, pg_attribute
            WHERE
                pg_class.oid = '%s'::regclass AND
                indrelid = pg_class.oid AND
                pg_attribute.attrelid = pg_class.oid AND
                pg_attribute.attnum = any(pg_index.indkey) AND
                indisprimary;
        """
        cursor = self.database.execute_sql(query % tbl)
        return [row[0] for row in cursor.fetchall()]

    @operation
    def set_search_path(self, schema_name):
        return (self
                .make_context()
                .literal('SET search_path TO %s' % schema_name))

    @operation
    def rename_table(self, old_name, new_name):
        pk_names = self._primary_key_columns(old_name)
        ParentClass = super(PostgresqlMigrator, self)

        operations = [
            ParentClass.rename_table(old_name, new_name, with_context=True)]

        if len(pk_names) == 1:
            # Check for existence of primary key sequence.
            seq_name = '%s_%s_seq' % (old_name, pk_names[0])
            query = """
                SELECT 1
                FROM information_schema.sequences
                WHERE LOWER(sequence_name) = LOWER(%s)
            """
            cursor = self.database.execute_sql(query, (seq_name,))
            if bool(cursor.fetchone()):
                new_seq_name = '%s_%s_seq' % (new_name, pk_names[0])
                operations.append(ParentClass.rename_table(
                    seq_name, new_seq_name))

        return operations


class CockroachDBMigrator(PostgresqlMigrator):
    explicit_create_foreign_key = True

    def add_inline_fk_sql(self, ctx, field):
        pass

    @operation
    def drop_index(self, table, index_name):
        return (self
                .make_context()
                .literal('DROP INDEX ')
                .sql(Entity(index_name))
                .literal(' CASCADE'))


class MySQLColumn(namedtuple('_Column', ('name', 'definition', 'null', 'pk',
                                         'default', 'extra'))):
    @property
    def is_pk(self):
        return self.pk == 'PRI'

    @property
    def is_unique(self):
        return self.pk == 'UNI'

    @property
    def is_null(self):
        return self.null == 'YES'

    def sql(self, column_name=None, is_null=None):
        if is_null is None:
            is_null = self.is_null
        if column_name is None:
            column_name = self.name
        parts = [
            Entity(column_name),
            SQL(self.definition)]
        if self.is_unique:
            parts.append(SQL('UNIQUE'))
        if is_null:
            parts.append(SQL('NULL'))
        else:
            parts.append(SQL('NOT NULL'))
        if self.is_pk:
            parts.append(SQL('PRIMARY KEY'))
        if self.extra:
            parts.append(SQL(self.extra))
        return NodeList(parts)


class MySQLMigrator(SchemaMigrator):
    explicit_create_foreign_key = True
    explicit_delete_foreign_key = True

    def _alter_column(self, ctx, table, column):
        return (self
                ._alter_table(ctx, table)
                .literal(' MODIFY ')
                .sql(Entity(column)))

    @operation
    def rename_table(self, old_name, new_name):
        return (self
                .make_context()
                .literal('RENAME TABLE ')
                .sql(Entity(old_name))
                .literal(' TO ')
                .sql(Entity(new_name)))

    def _get_column_definition(self, table, column_name):
        cursor = self.database.execute_sql('DESCRIBE `%s`;' % table)
        rows = cursor.fetchall()
        for row in rows:
            column = MySQLColumn(*row)
            if column.name == column_name:
                return column
        return False

    def get_foreign_key_constraint(self, table, column_name):
        cursor = self.database.execute_sql(
            ('SELECT constraint_name '
             'FROM information_schema.key_column_usage WHERE '
             'table_schema = DATABASE() AND '
             'table_name = %s AND '
             'column_name = %s AND '
             'referenced_table_name IS NOT NULL AND '
             'referenced_column_name IS NOT NULL;'),
            (table, column_name))
        result = cursor.fetchone()
        if not result:
            raise AttributeError(
                'Unable to find foreign key constraint for '
                '"%s" on table "%s".' % (table, column_name))
        return result[0]

    @operation
    def drop_foreign_key_constraint(self, table, column_name):
        fk_constraint = self.get_foreign_key_constraint(table, column_name)
        return (self
                ._alter_table(self.make_context(), table)
                .literal(' DROP FOREIGN KEY ')
                .sql(Entity(fk_constraint)))

    def add_inline_fk_sql(self, ctx, field):
        pass

    @operation
    def add_not_null(self, table, column):
        column_def = self._get_column_definition(table, column)
        add_not_null = (self
                        ._alter_table(self.make_context(), table)
                        .literal(' MODIFY ')
                        .sql(column_def.sql(is_null=False)))

        fk_objects = dict(
            (fk.column, fk)
            for fk in self.database.get_foreign_keys(table))
        if column not in fk_objects:
            return add_not_null

        fk_metadata = fk_objects[column]
        return (self.drop_foreign_key_constraint(table, column),
                add_not_null,
                self.add_foreign_key_constraint(
                    table,
                    column,
                    fk_metadata.dest_table,
                    fk_metadata.dest_column))

    @operation
    def drop_not_null(self, table, column):
        column = self._get_column_definition(table, column)
        if column.is_pk:
            raise ValueError('Primary keys can not be null')
        return (self
                ._alter_table(self.make_context(), table)
                .literal(' MODIFY ')
                .sql(column.sql(is_null=True)))

    @operation
    def rename_column(self, table, old_name, new_name):
        fk_objects = dict(
            (fk.column, fk)
            for fk in self.database.get_foreign_keys(table))
        is_foreign_key = old_name in fk_objects

        column = self._get_column_definition(table, old_name)
        rename_ctx = (self
                      ._alter_table(self.make_context(), table)
                      .literal(' CHANGE ')
                      .sql(Entity(old_name))
                      .literal(' ')
                      .sql(column.sql(column_name=new_name)))
        if is_foreign_key:
            fk_metadata = fk_objects[old_name]
            return [
                self.drop_foreign_key_constraint(table, old_name),
                rename_ctx,
                self.add_foreign_key_constraint(
                    table,
                    new_name,
                    fk_metadata.dest_table,
                    fk_metadata.dest_column),
            ]
        else:
            return rename_ctx

    @operation
    def alter_column_type(self, table, column, field, cast=None):
        if cast is not None:
            raise ValueError('alter_column_type() does not support cast with '
                             'MySQL.')
        ctx = self.make_context()
        return (self
                ._alter_table(ctx, table)
                .literal(' MODIFY ')
                .sql(Entity(column))
                .literal(' ')
                .sql(field.ddl(ctx)))

    @operation
    def drop_index(self, table, index_name):
        return (self
                .make_context()
                .literal('DROP INDEX ')
                .sql(Entity(index_name))
                .literal(' ON ')
                .sql(Entity(table)))


class SqliteMigrator(SchemaMigrator):
    """
    SQLite supports a subset of ALTER TABLE queries, view the docs for the
    full details http://sqlite.org/lang_altertable.html
    """
    column_re = re.compile(r'(.+?)\((.+)\)')
    column_split_re = re.compile(r'(?:[^,(]|\([^)]*\))+')
    column_name_re = re.compile(r'''["`']?([\w]+)''')
    fk_re = re.compile(r'FOREIGN KEY\s+\("?([\w]+)"?\)\s+', re.I)

    def _get_column_names(self, table):
        res = self.database.execute_sql('select * from "%s" limit 1' % table)
        return [item[0] for item in res.description]

    def _get_create_table(self, table):
        res = self.database.execute_sql(
            ('select name, sql from sqlite_master '
             'where type=? and LOWER(name)=?'),
            ['table', table.lower()])
        return res.fetchone()

    @operation
    def _update_column(self, table, column_to_update, fn):
        columns = set(column.name.lower()
                      for column in self.database.get_columns(table))
        if column_to_update.lower() not in columns:
            raise ValueError('Column "%s" does not exist on "%s"' %
                             (column_to_update, table))

        # Get the SQL used to create the given table.
        table, create_table = self._get_create_table(table)

        # Get the indexes and SQL to re-create indexes.
        indexes = self.database.get_indexes(table)

        # Find any foreign keys we may need to remove.
        self.database.get_foreign_keys(table)

        # Make sure the create_table does not contain any newlines or tabs,
        # allowing the regex to work correctly.
        create_table = re.sub(r'\s+', ' ', create_table)

        # Parse out the `CREATE TABLE` and column list portions of the query.
        raw_create, raw_columns = self.column_re.search(create_table).groups()

        # Clean up the individual column definitions.
        split_columns = self.column_split_re.findall(raw_columns)
        column_defs = [col.strip() for col in split_columns]

        new_column_defs = []
        new_column_names = []
        original_column_names = []
        constraint_terms = ('foreign ', 'primary ', 'constraint ', 'check ')

        for column_def in column_defs:
            column_name, = self.column_name_re.match(column_def).groups()

            if column_name == column_to_update:
                new_column_def = fn(column_name, column_def)
                if new_column_def:
                    new_column_defs.append(new_column_def)
                    original_column_names.append(column_name)
                    column_name, = self.column_name_re.match(
                        new_column_def).groups()
                    new_column_names.append(column_name)
            else:
                new_column_defs.append(column_def)

                # Avoid treating constraints as columns.
                if not column_def.lower().startswith(constraint_terms):
                    new_column_names.append(column_name)
                    original_column_names.append(column_name)

        # Create a mapping of original columns to new columns.
        original_to_new = dict(zip(original_column_names, new_column_names))
        new_column = original_to_new.get(column_to_update)

        fk_filter_fn = lambda column_def: column_def
        if not new_column:
            # Remove any foreign keys associated with this column.
            fk_filter_fn = lambda column_def: None
        elif new_column != column_to_update:
            # Update any foreign keys for this column.
            fk_filter_fn = lambda column_def: self.fk_re.sub(
                'FOREIGN KEY ("%s") ' % new_column,
                column_def)

        cleaned_columns = []
        for column_def in new_column_defs:
            match = self.fk_re.match(column_def)
            if match is not None and match.groups()[0] == column_to_update:
                column_def = fk_filter_fn(column_def)
            if column_def:
                cleaned_columns.append(column_def)

        # Update the name of the new CREATE TABLE query.
        temp_table = table + '__tmp__'
        rgx = re.compile('("?)%s("?)' % table, re.I)
        create = rgx.sub(
            '\\1%s\\2' % temp_table,
            raw_create)

        # Create the new table.
        columns = ', '.join(cleaned_columns)
        queries = [
            NodeList([SQL('DROP TABLE IF EXISTS'), Entity(temp_table)]),
            SQL('%s (%s)' % (create.strip(), columns))]

        # Populate new table.
        populate_table = NodeList((
            SQL('INSERT INTO'),
            Entity(temp_table),
            EnclosedNodeList([Entity(col) for col in new_column_names]),
            SQL('SELECT'),
            CommaNodeList([Entity(col) for col in original_column_names]),
            SQL('FROM'),
            Entity(table)))
        drop_original = NodeList([SQL('DROP TABLE'), Entity(table)])

        # Drop existing table and rename temp table.
        queries += [
            populate_table,
            drop_original,
            self.rename_table(temp_table, table)]

        # Re-create user-defined indexes. User-defined indexes will have a
        # non-empty SQL attribute.
        for index in filter(lambda idx: idx.sql, indexes):
            if column_to_update not in index.columns:
                queries.append(SQL(index.sql))
            elif new_column:
                sql = self._fix_index(index.sql, column_to_update, new_column)
                if sql is not None:
                    queries.append(SQL(sql))

        return queries

    def _fix_index(self, sql, column_to_update, new_column):
        # Split on the name of the column to update. If it splits into two
        # pieces, then there's no ambiguity and we can simply replace the
        # old with the new.
        parts = sql.split(column_to_update)
        if len(parts) == 2:
            return sql.replace(column_to_update, new_column)

        # Find the list of columns in the index expression.
        lhs, rhs = sql.rsplit('(', 1)

        # Apply the same "split in two" logic to the column list portion of
        # the query.
        if len(rhs.split(column_to_update)) == 2:
            return '%s(%s' % (lhs, rhs.replace(column_to_update, new_column))

        # Strip off the trailing parentheses and go through each column.
        parts = rhs.rsplit(')', 1)[0].split(',')
        columns = [part.strip('"`[]\' ') for part in parts]

        # `columns` looks something like: ['status', 'timestamp" DESC']
        # https://www.sqlite.org/lang_keywords.html
        # Strip out any junk after the column name.
        clean = []
        for column in columns:
            if re.match(r'%s(?:[\'"`\]]?\s|$)' % column_to_update, column):
                column = new_column + column[len(column_to_update):]
            clean.append(column)

        return '%s(%s)' % (lhs, ', '.join('"%s"' % c for c in clean))

    @operation
    def drop_column(self, table, column_name, cascade=True, legacy=False):
        if sqlite3.sqlite_version_info >= (3, 35, 0) and not legacy:
            ctx = self.make_context()
            (self._alter_table(ctx, table)
             .literal(' DROP COLUMN ')
             .sql(Entity(column_name)))
            return ctx
        return self._update_column(table, column_name, lambda a, b: None)

    @operation
    def rename_column(self, table, old_name, new_name, legacy=False):
        if sqlite3.sqlite_version_info >= (3, 25, 0) and not legacy:
            return (self
                    ._alter_table(self.make_context(), table)
                    .literal(' RENAME COLUMN ')
                    .sql(Entity(old_name))
                    .literal(' TO ')
                    .sql(Entity(new_name)))
        def _rename(column_name, column_def):
            return column_def.replace(column_name, new_name)
        return self._update_column(table, old_name, _rename)

    @operation
    def add_not_null(self, table, column):
        def _add_not_null(column_name, column_def):
            return column_def + ' NOT NULL'
        return self._update_column(table, column, _add_not_null)

    @operation
    def drop_not_null(self, table, column):
        def _drop_not_null(column_name, column_def):
            return column_def.replace('NOT NULL', '')
        return self._update_column(table, column, _drop_not_null)

    @operation
    def add_column_default(self, table, column, default):
        if default is None:
            raise ValueError('`default` must be not None/NULL.')
        if callable_(default):
            default = default()
        if (isinstance(default, str) and not default.endswith((')', "'"))
            and not default.isdigit()):
            default = "'%s'" % default
        def _add_default(column_name, column_def):
            # Try to handle SQL functions and string literals, otherwise quote.
            return column_def + ' DEFAULT %s' % default
        return self._update_column(table, column, _add_default)

    @operation
    def drop_column_default(self, table, column):
        def _drop_default(column_name, column_def):
            col = re.sub(r'DEFAULT\s+[\w"\'\(\)]+(\s|$)', '', column_def, re.I)
            return col.strip()
        return self._update_column(table, column, _drop_default)

    @operation
    def alter_column_type(self, table, column, field, cast=None):
        if cast is not None:
            raise ValueError('alter_column_type() does not support cast with '
                             'Sqlite.')
        ctx = self.make_context()
        def _alter_column_type(column_name, column_def):
            node_list = field.ddl(ctx)
            sql, _ = ctx.sql(Entity(column)).sql(node_list).query()
            return sql
        return self._update_column(table, column, _alter_column_type)

    @operation
    def add_constraint(self, table, name, constraint):
        raise NotImplementedError

    @operation
    def drop_constraint(self, table, name):
        raise NotImplementedError

    @operation
    def add_foreign_key_constraint(self, table, column_name, field,
                                   on_delete=None, on_update=None):
        raise NotImplementedError


def migrate(*operations, **kwargs):
    for operation in operations:
        operation.run()
