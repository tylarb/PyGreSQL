#! /usr/bin/python
#
# pg.py
#
# $Id$
#

"""PyGreSQL classic interface.

This pg module implements some basic database management stuff.
It includes the _pg module and builds on it, providing the higher
level wrapper class named DB with additional functionality.
This is known as the "classic" ("old style") PyGreSQL interface.
For a DB-API 2 compliant interface use the newer pgdb module.
"""

# Copyright (c) 1997-2016 by D'Arcy J.M. Cain.
#
# Contributions made by Ch. Zwerschke and others.
#
# The notification handler is based on pgnotify which is
# Copyright (c) 2001 Ng Pheng Siong. All rights reserved.
#
# Permission to use, copy, modify, and distribute this software and its
# documentation for any purpose and without fee is hereby granted,
# provided that the above copyright notice appear in all copies and that
# both that copyright notice and this permission notice appear in
# supporting documentation.

from __future__ import print_function

from _pg import *

import select
import warnings

from decimal import Decimal
from collections import namedtuple
from functools import partial
from operator import itemgetter
from json import loads as jsondecode, dumps as jsonencode

try:
    basestring
except NameError:  # Python >= 3.0
    basestring = (str, bytes)

try:
    from collections import OrderedDict
except ImportError:  # Python 2.6 or 3.0
    OrderedDict = dict


    class AttrDict(dict):
        """Simple read-only ordered dictionary for storing attribute names."""

        def __init__(self, *args, **kw):
            if len(args) > 1 or kw:
                raise TypeError
            items = args[0] if args else []
            if isinstance(items, dict):
                raise TypeError
            items = list(items)
            self._keys = [item[0] for item in items]
            dict.__init__(self, items)
            self._read_only = True
            error = self._read_only_error
            self.clear = self.update = error
            self.pop = self.setdefault = self.popitem = error

        def __setitem__(self, key, value):
            if self._read_only:
                self._read_only_error()
            dict.__setitem__(self, key, value)

        def __delitem__(self, key):
            if self._read_only:
                self._read_only_error()
            dict.__delitem__(self, key)

        def __iter__(self):
            return iter(self._keys)

        def keys(self):
            return list(self._keys)

        def values(self):
            return [self[key] for key in self]

        def items(self):
            return [(key, self[key]) for key in self]

        def iterkeys(self):
            return self.__iter__()

        def itervalues(self):
            return iter(self.values())

        def iteritems(self):
            return iter(self.items())

        @staticmethod
        def _read_only_error(*args, **kw):
            raise TypeError('This object is read-only')

else:

     class AttrDict(OrderedDict):
        """Simple read-only ordered dictionary for storing attribute names."""

        def __init__(self, *args, **kw):
            self._read_only = False
            OrderedDict.__init__(self, *args, **kw)
            self._read_only = True
            error = self._read_only_error
            self.clear = self.update = error
            self.pop = self.setdefault = self.popitem = error

        def __setitem__(self, key, value):
            if self._read_only:
                self._read_only_error()
            OrderedDict.__setitem__(self, key, value)

        def __delitem__(self, key):
            if self._read_only:
                self._read_only_error()
            OrderedDict.__delitem__(self, key)

        @staticmethod
        def _read_only_error(*args, **kw):
            raise TypeError('This object is read-only')


# Auxiliary functions that are independent from a DB connection:

def _oid_key(table):
    """Build oid key from a table name."""
    return 'oid(%s)' % table


def _simpletype(typ):
    """Determine a simplified name a pg_type name."""
    if typ.startswith('bool'):
        return 'bool'
    if typ.startswith(('abstime', 'date', 'interval', 'timestamp')):
        return 'date'
    if typ.startswith(('cid', 'oid', 'int', 'xid')):
        return 'int'
    if typ.startswith('float'):
        return 'float'
    if typ.startswith('numeric'):
        return 'num'
    if typ.startswith('money'):
        return 'money'
    if typ.startswith('bytea'):
        return 'bytea'
    if typ.startswith('json'):
        return 'json'
    return 'text'


def _namedresult(q):
    """Get query result as named tuples."""
    row = namedtuple('Row', q.listfields())
    return [row(*r) for r in q.getresult()]


class _MemoryQuery:
    """Class that embodies a given query result."""

    def __init__(self, result, fields):
        """Create query from given result rows and field names."""
        self.result = result
        self.fields = fields

    def listfields(self):
        """Return the stored field names of this query."""
        return self.fields

    def getresult(self):
        """Return the stored result of this query."""
        return self.result


def _db_error(msg, cls=DatabaseError):
    """Return DatabaseError with empty sqlstate attribute."""
    error = cls(msg)
    error.sqlstate = None
    return error


def _int_error(msg):
    """Return InternalError."""
    return _db_error(msg, InternalError)


def _prg_error(msg):
    """Return ProgrammingError."""
    return _db_error(msg, ProgrammingError)


# Initialize the C module

set_namedresult(_namedresult)
set_decimal(Decimal)
set_jsondecode(jsondecode)


# The notification handler

class NotificationHandler(object):
    """A PostgreSQL client-side asynchronous notification handler."""

    def __init__(self, db, event, callback=None,
            arg_dict=None, timeout=None, stop_event=None):
        """Initialize the notification handler.

        You must pass a PyGreSQL database connection, the name of an
        event (notification channel) to listen for and a callback function.

        You can also specify a dictionary arg_dict that will be passed as
        the single argument to the callback function, and a timeout value
        in seconds (a floating point number denotes fractions of seconds).
        If it is absent or None, the callers will never time out.  If the
        timeout is reached, the callback function will be called with a
        single argument that is None.  If you set the timeout to zero,
        the handler will poll notifications synchronously and return.

        You can specify the name of the event that will be used to signal
        the handler to stop listening as stop_event. By default, it will
        be the event name prefixed with 'stop_'.
        """
        self.db = db
        self.event = event
        self.stop_event = stop_event or 'stop_%s' % event
        self.listening = False
        self.callback = callback
        if arg_dict is None:
            arg_dict = {}
        self.arg_dict = arg_dict
        self.timeout = timeout

    def __del__(self):
        self.unlisten()

    def close(self):
        """Stop listening and close the connection."""
        if self.db:
            self.unlisten()
            self.db.close()
            self.db = None

    def listen(self):
        """Start listening for the event and the stop event."""
        if not self.listening:
            self.db.query('listen "%s"' % self.event)
            self.db.query('listen "%s"' % self.stop_event)
            self.listening = True

    def unlisten(self):
        """Stop listening for the event and the stop event."""
        if self.listening:
            self.db.query('unlisten "%s"' % self.event)
            self.db.query('unlisten "%s"' % self.stop_event)
            self.listening = False

    def notify(self, db=None, stop=False, payload=None):
        """Generate a notification.

        Optionally, you can pass a payload with the notification.

        If you set the stop flag, a stop notification will be sent that
        will cause the handler to stop listening.

        Note: If the notification handler is running in another thread, you
        must pass a different database connection since PyGreSQL database
        connections are not thread-safe.
        """
        if self.listening:
            if not db:
                db = self.db
            q = 'notify "%s"' % (self.stop_event if stop else self.event)
            if payload:
                q += ", '%s'" % payload
            return db.query(q)

    def __call__(self):
        """Invoke the notification handler.

        The handler is a loop that listens for notifications on the event
        and stop event channels.  When either of these notifications are
        received, its associated 'pid', 'event' and 'extra' (the payload
        passed with the notification) are inserted into its arg_dict
        dictionary and the callback is invoked with this dictionary as
        a single argument.  When the handler receives a stop event, it
        stops listening to both events and return.

        In the special case that the timeout of the handler has been set
        to zero, the handler will poll all events synchronously and return.
        If will keep listening until it receives a stop event.

        Note: If you run this loop in another thread, don't use the same
        database connection for database operations in the main thread.
        """
        self.listen()
        poll = self.timeout == 0
        if not poll:
            rlist = [self.db.fileno()]
        while self.listening:
            if poll or select.select(rlist, [], [], self.timeout)[0]:
                while self.listening:
                    notice = self.db.getnotify()
                    if not notice:  # no more messages
                        break
                    event, pid, extra = notice
                    if event not in (self.event, self.stop_event):
                        self.unlisten()
                        raise _db_error(
                            'Listening for "%s" and "%s", but notified of "%s"'
                            % (self.event, self.stop_event, event))
                    if event == self.stop_event:
                        self.unlisten()
                    self.arg_dict.update(pid=pid, event=event, extra=extra)
                    self.callback(self.arg_dict)
                if poll:
                    break
            else:   # we timed out
                self.unlisten()
                self.callback(None)


def pgnotify(*args, **kw):
    """Same as NotificationHandler, under the traditional name."""
    warnings.warn("pgnotify is deprecated, use NotificationHandler instead",
        DeprecationWarning, stacklevel=2)
    return NotificationHandler(*args, **kw)


# The actual PostGreSQL database connection interface:

class DB(object):
    """Wrapper class for the _pg connection type."""

    def __init__(self, *args, **kw):
        """Create a new connection

        You can pass either the connection parameters or an existing
        _pg or pgdb connection. This allows you to use the methods
        of the classic pg interface with a DB-API 2 pgdb connection.
        """
        if not args and len(kw) == 1:
            db = kw.get('db')
        elif not kw and len(args) == 1:
            db = args[0]
        else:
            db = None
        if db:
            if isinstance(db, DB):
                db = db.db
            else:
                try:
                    db = db._cnx
                except AttributeError:
                    pass
        if not db or not hasattr(db, 'db') or not hasattr(db, 'query'):
            db = connect(*args, **kw)
            self._closeable = True
        else:
            self._closeable = False
        self.db = db
        self.dbname = db.db
        self._regtypes = False
        self._attnames = {}
        self._pkeys = {}
        self._privileges = {}
        self._args = args, kw
        self.debug = None  # For debugging scripts, this can be set
            # * to a string format specification (e.g. in CGI set to "%s<BR>"),
            # * to a file object to write debug statements or
            # * to a callable object which takes a string argument
            # * to any other true value to just print debug statements

    def __getattr__(self, name):
        # All undefined members are same as in underlying connection:
        if self.db:
            return getattr(self.db, name)
        else:
            raise _int_error('Connection is not valid')

    def __dir__(self):
        # Custom dir function including the attributes of the connection:
        attrs = set(self.__class__.__dict__)
        attrs.update(self.__dict__)
        attrs.update(dir(self.db))
        return sorted(attrs)

    # Context manager methods

    def __enter__(self):
        """Enter the runtime context. This will start a transactio."""
        self.begin()
        return self

    def __exit__(self, et, ev, tb):
        """Exit the runtime context. This will end the transaction."""
        if et is None and ev is None and tb is None:
            self.commit()
        else:
            self.rollback()

    # Auxiliary methods

    def _do_debug(self, *args):
        """Print a debug message"""
        if self.debug:
            s = '\n'.join(args)
            if isinstance(self.debug, basestring):
                print(self.debug % s)
            elif hasattr(self.debug, 'write'):
                self.debug.write(s + '\n')
            elif callable(self.debug):
                self.debug(s)
            else:
                print(s)

    def _escape_qualified_name(self, s):
        """Escape a qualified name.

        Escapes the name for use as an SQL identifier, unless the
        name contains a dot, in which case the name is ambiguous
        (could be a qualified name or just a name with a dot in it)
        and must be quoted manually by the caller.
        """
        if '.' not in s:
            s = self.escape_identifier(s)
        return s

    @staticmethod
    def _make_bool(d):
        """Get boolean value corresponding to d."""
        return bool(d) if get_bool() else ('t' if d else 'f')

    _bool_true_values = frozenset('t true 1 y yes on'.split())

    def _prepare_bool(self, d):
        """Prepare a boolean parameter."""
        if isinstance(d, basestring):
            if not d:
                return None
            d = d.lower() in self._bool_true_values
        return 't' if d else 'f'

    _date_literals = frozenset('current_date current_time'
        ' current_timestamp localtime localtimestamp'.split())

    def _prepare_date(self, d):
        """Prepare a date parameter."""
        if not d:
            return None
        if isinstance(d, basestring) and d.lower() in self._date_literals:
            raise ValueError
        return d

    _num_types = frozenset('int float num money'
        ' int2 int4 int8 float4 float8 numeric money'.split())

    def _prepare_num(self, d):
        """Prepare a numeric parameter."""
        if not d and d != 0:
            return None
        return d

    def _prepare_bytea(self, d):
        """Prepare a bytea parameter."""
        return self.escape_bytea(d)

    def _prepare_json(self, d):
        """Prepare a json parameter."""
        return self.encode_json(d)

    _prepare_funcs = dict(  # quote methods for each type
        bool=_prepare_bool, date=_prepare_date,
        int=_prepare_num, num=_prepare_num, float=_prepare_num,
        money=_prepare_num, bytea=_prepare_bytea, json=_prepare_json)

    def _prepare_param(self, value, typ, params):
        """Prepare and add a parameter to the list."""
        if value is not None and typ != 'text':
            prepare = self._prepare_funcs[typ]
            try:
                value = prepare(self, value)
            except ValueError:
                return value
        params.append(value)
        return '$%d' % len(params)

    def _list_params(self, params):
        """Create a human readable parameter list."""
        return ', '.join('$%d=%r' % (n, v) for n, v in enumerate(params, 1))

    @staticmethod
    def _prepare_qualified_param(name, param):
        """Quote parameter representing a qualified name.

        Escapes the name for use as an SQL parameter, unless the
        name contains a dot, in which case the name is ambiguous
        (could be a qualified name or just a name with a dot in it)
        and must be quoted manually by the caller.

        """
        if isinstance(param, int):
            param = "$%d" % param
        if '.' not in name:
            param = 'quote_ident(%s)' % (param,)
        return param

    # Public methods

    # escape_string and escape_bytea exist as methods,
    # so we define unescape_bytea as a method as well
    unescape_bytea = staticmethod(unescape_bytea)

    def decode_json(self, s):
        """Decode a JSON string coming from the database."""
        return (get_jsondecode() or jsondecode)(s)

    def encode_json(self, d):
        """Encode a JSON string for use within SQL."""
        return jsonencode(d)

    def close(self):
        """Close the database connection."""
        # Wraps shared library function so we can track state.
        if self._closeable:
            if self.db:
                self.db.close()
                self.db = None
            else:
                raise _int_error('Connection already closed')

    def reset(self):
        """Reset connection with current parameters.

        All derived queries and large objects derived from this connection
        will not be usable after this call.

        """
        if self.db:
            self.db.reset()
        else:
            raise _int_error('Connection already closed')

    def reopen(self):
        """Reopen connection to the database.

        Used in case we need another connection to the same database.
        Note that we can still reopen a database that we have closed.

        """
        # There is no such shared library function.
        if self._closeable:
            db = connect(*self._args[0], **self._args[1])
            if self.db:
                self.db.close()
            self.db = db

    def begin(self, mode=None):
        """Begin a transaction."""
        qstr = 'BEGIN'
        if mode:
            qstr += ' ' + mode
        return self.query(qstr)

    start = begin

    def commit(self):
        """Commit the current transaction."""
        return self.query('COMMIT')

    end = commit

    def rollback(self, name=None):
        """Roll back the current transaction."""
        qstr = 'ROLLBACK'
        if name:
            qstr += ' TO ' + name
        return self.query(qstr)

    abort = rollback

    def savepoint(self, name):
        """Define a new savepoint within the current transaction."""
        return self.query('SAVEPOINT ' + name)

    def release(self, name):
        """Destroy a previously defined savepoint."""
        return self.query('RELEASE ' + name)

    def get_parameter(self, parameter):
        """Get the value of a run-time parameter.

        If the parameter is a string, the return value will also be a string
        that is the current setting of the run-time parameter with that name.

        You can get several parameters at once by passing a list, set or dict.
        When passing a list of parameter names, the return value will be a
        corresponding list of parameter settings.  When passing a set of
        parameter names, a new dict will be returned, mapping these parameter
        names to their settings.  Finally, if you pass a dict as parameter,
        its values will be set to the current parameter settings corresponding
        to its keys.

        By passing the special name 'all' as the parameter, you can get a dict
        of all existing configuration parameters.
        """
        if isinstance(parameter, basestring):
            parameter = [parameter]
            values = None
        elif isinstance(parameter, (list, tuple)):
            values = []
        elif isinstance(parameter, (set, frozenset)):
            values = {}
        elif isinstance(parameter, dict):
            values = parameter
        else:
            raise TypeError(
                'The parameter must be a string, list, set or dict')
        if not parameter:
            raise TypeError('No parameter has been specified')
        params = {} if isinstance(values, dict) else []
        for key in parameter:
            param = key.strip().lower() if isinstance(
                key, basestring) else None
            if not param:
                raise TypeError('Invalid parameter')
            if param == 'all':
                q = 'SHOW ALL'
                values = self.db.query(q).getresult()
                values = dict(value[:2] for value in values)
                break
            if isinstance(values, dict):
                params[param] = key
            else:
                params.append(param)
        else:
            for param in params:
                q = 'SHOW %s' % (param,)
                value = self.db.query(q).getresult()[0][0]
                if values is None:
                    values = value
                elif isinstance(values, list):
                    values.append(value)
                else:
                    values[params[param]] = value
        return values

    def set_parameter(self, parameter, value=None, local=False):
        """Set the value of a run-time parameter.

        If the parameter and the value are strings, the run-time parameter
        will be set to that value.  If no value or None is passed as a value,
        then the run-time parameter will be restored to its default value.

        You can set several parameters at once by passing a list of parameter
        names, together with a single value that all parameters should be
        set to or with a corresponding list of values.  You can also pass
        the parameters as a set if you only provide a single value.
        Finally, you can pass a dict with parameter names as keys.  In this
        case, you should not pass a value, since the values for the parameters
        will be taken from the dict.

        By passing the special name 'all' as the parameter, you can reset
        all existing settable run-time parameters to their default values.

        If you set local to True, then the command takes effect for only the
        current transaction.  After commit() or rollback(), the session-level
        setting takes effect again.  Setting local to True will appear to
        have no effect if it is executed outside a transaction, since the
        transaction will end immediately.
        """
        if isinstance(parameter, basestring):
            parameter = {parameter: value}
        elif isinstance(parameter, (list, tuple)):
            if isinstance(value, (list, tuple)):
                parameter = dict(zip(parameter, value))
            else:
                parameter = dict.fromkeys(parameter, value)
        elif isinstance(parameter, (set, frozenset)):
            if isinstance(value, (list, tuple, set, frozenset)):
                value = set(value)
                if len(value) == 1:
                    value = value.pop()
            if not(value is None or isinstance(value, basestring)):
                raise ValueError('A single value must be specified'
                    ' when parameter is a set')
            parameter = dict.fromkeys(parameter, value)
        elif isinstance(parameter, dict):
            if value is not None:
                raise ValueError('A value must not be specified'
                    ' when parameter is a dictionary')
        else:
            raise TypeError(
                'The parameter must be a string, list, set or dict')
        if not parameter:
            raise TypeError('No parameter has been specified')
        params = {}
        for key, value in parameter.items():
            param = key.strip().lower() if isinstance(
                key, basestring) else None
            if not param:
                raise TypeError('Invalid parameter')
            if param == 'all':
                if value is not None:
                    raise ValueError('A value must ot be specified'
                        " when parameter is 'all'")
                params = {'all': None}
                break
            params[param] = value
        local = ' LOCAL' if local else ''
        for param, value in params.items():
            if value is None:
                q = 'RESET%s %s' % (local, param)
            else:
                q = 'SET%s %s TO %s' % (local, param, value)
            self._do_debug(q)
            self.db.query(q)

    def query(self, qstr, *args):
        """Execute a SQL command string.

        This method simply sends a SQL query to the database.  If the query is
        an insert statement that inserted exactly one row into a table that
        has OIDs, the return value is the OID of the newly inserted row.
        If the query is an update or delete statement, or an insert statement
        that did not insert exactly one row in a table with OIDs, then the
        number of rows affected is returned as a string.  If it is a statement
        that returns rows as a result (usually a select statement, but maybe
        also an "insert/update ... returning" statement), this method returns
        a Query object that can be accessed via getresult() or dictresult()
        or simply printed.  Otherwise, it returns `None`.

        The query can contain numbered parameters of the form $1 in place
        of any data constant.  Arguments given after the query string will
        be substituted for the corresponding numbered parameter.  Parameter
        values can also be given as a single list or tuple argument.
        """
        # Wraps shared library function for debugging.
        if not self.db:
            raise _int_error('Connection is not valid')
        self._do_debug(qstr)
        return self.db.query(qstr, args)

    def pkey(self, table, composite=False, flush=False):
        """Get or set the primary key of a table.

        Single primary keys are returned as strings unless you
        set the composite flag.  Composite primary keys are always
        represented as tuples.  Note that this raises a KeyError
        if the table does not have a primary key.

        If flush is set then the internal cache for primary keys will
        be flushed.  This may be necessary after the database schema or
        the search path has been changed.
        """
        pkeys = self._pkeys
        if flush:
            pkeys.clear()
            self._do_debug('The pkey cache has been flushed')
        try:  # cache lookup
            pkey = pkeys[table]
        except KeyError:  # cache miss, check the database
            q = ("SELECT a.attname, a.attnum, i.indkey FROM pg_index i"
                " JOIN pg_attribute a ON a.attrelid = i.indrelid"
                " AND a.attnum = ANY(i.indkey)"
                " AND NOT a.attisdropped"
                " WHERE i.indrelid=%s::regclass"
                " AND i.indisprimary ORDER BY a.attnum") % (
                    self._prepare_qualified_param(table, 1),)
            pkey = self.db.query(q, (table,)).getresult()
            if not pkey:
                raise KeyError('Table %s has no primary key' % table)
            # we want to use the order defined in the primary key index here,
            # not the order as defined by the columns in the table
            if len(pkey) > 1:
                indkey = [int(k) for k in pkey[0][2].split()]
                pkey = sorted(pkey, key=lambda row: indkey.index(row[1]))
                pkey = tuple(row[0] for row in pkey)
            else:
                pkey = pkey[0][0]
            pkeys[table] = pkey  # cache it
        if composite and not isinstance(pkey, tuple):
            pkey = (pkey,)
        return pkey

    def get_databases(self):
        """Get list of databases in the system."""
        return [s[0] for s in
            self.db.query('SELECT datname FROM pg_database').getresult()]

    def get_relations(self, kinds=None):
        """Get list of relations in connected database of specified kinds.

        If kinds is None or empty, all kinds of relations are returned.
        Otherwise kinds can be a string or sequence of type letters
        specifying which kind of relations you want to list.
        """
        where = " AND r.relkind IN (%s)" % ','.join(
            ["'%s'" % k for k in kinds]) if kinds else ''
        q = ("SELECT quote_ident(s.nspname)||'.'||quote_ident(r.relname)"
            " FROM pg_class r"
            " JOIN pg_namespace s ON s.oid = r.relnamespace"
            " WHERE s.nspname NOT SIMILAR"
            " TO 'pg/_%%|information/_schema' ESCAPE '/' %s"
            " ORDER BY s.nspname, r.relname") % where
        return [r[0] for r in self.db.query(q).getresult()]

    def get_tables(self):
        """Return list of tables in connected database."""
        return self.get_relations('r')

    def get_attnames(self, table, flush=False):
        """Given the name of a table, dig out the set of attribute names.

        Returns a read-only dictionary of attribute names (the names are
        the keys, the values are the names of the attributes' types)
        with the column names in the proper order if you iterate over it.

        If flush is set, then the internal cache for attribute names will
        be flushed. This may be necessary after the database schema or
        the search path has been changed.

        By default, only a limited number of simple types will be returned.
        You can get the regular types after calling use_regtypes(True).
        """
        attnames = self._attnames
        if flush:
            attnames.clear()
            self._do_debug('The attnames cache has been flushed')
        try:  # cache lookup
            names = attnames[table]
        except KeyError:  # cache miss, check the database
            q = ("SELECT a.attname, t.typname%s"
                " FROM pg_attribute a"
                " JOIN pg_type t ON t.oid = a.atttypid"
                " WHERE a.attrelid = %s::regclass"
                " AND (a.attnum > 0 OR a.attname = 'oid')"
                " AND NOT a.attisdropped ORDER BY a.attnum") % (
                    '::regtype' if self._regtypes else '',
                    self._prepare_qualified_param(table, 1))
            names = self.db.query(q, (table,)).getresult()
            if not self._regtypes:
                names = ((name, _simpletype(typ)) for name, typ in names)
            names = AttrDict(names)
            attnames[table] = names  # cache it
        return names

    def use_regtypes(self, regtypes=None):
        """Use regular type names instead of simplified type names."""
        if regtypes is None:
            return self._regtypes
        else:
            regtypes = bool(regtypes)
            if regtypes != self._regtypes:
                self._regtypes = regtypes
                self._attnames.clear()
            return regtypes

    def has_table_privilege(self, table, privilege='select'):
        """Check whether current user has specified table privilege."""
        privilege = privilege.lower()
        try:  # ask cache
            return self._privileges[(table, privilege)]
        except KeyError:  # cache miss, ask the database
            q = "SELECT has_table_privilege(%s, $2)" % (
                self._prepare_qualified_param(table, 1),)
            q = self.db.query(q, (table, privilege))
            ret = q.getresult()[0][0] == self._make_bool(True)
            self._privileges[(table, privilege)] = ret  # cache it
            return ret

    def get(self, table, row, keyname=None):
        """Get a row from a database table or view.

        This method is the basic mechanism to get a single row.  It assumes
        that the keyname specifies a unique row.  It must be the name of a
        single column or a tuple of column names.  If the keyname is not
        specified, then the primary key for the table is used.

        If row is a dictionary, then the value for the key is taken from it.
        Otherwise, the row must be a single value or a tuple of values
        corresponding to the passed keyname or primary key.  The fetched row
        from the table will be returned as a new dictionary or used to replace
        the existing values when row was passed as aa dictionary.

        The OID is also put into the dictionary if the table has one, but
        in order to allow the caller to work with multiple tables, it is
        munged as "oid(table)" using the actual name of the table.
        """
        if table.endswith('*'):  # hint for descendant tables can be ignored
            table = table[:-1].rstrip()
        attnames = self.get_attnames(table)
        qoid = _oid_key(table) if 'oid' in attnames else None
        if keyname and isinstance(keyname, basestring):
            keyname = (keyname,)
        if qoid and isinstance(row, dict) and qoid in row and 'oid' not in row:
            row['oid'] = row[qoid]
        if not keyname:
            try:  # if keyname is not specified, try using the primary key
                keyname = self.pkey(table, True)
            except KeyError:  # the table has no primary key
                # try using the oid instead
                if qoid and isinstance(row, dict) and 'oid' in row:
                    keyname = ('oid',)
                else:
                    raise _prg_error('Table %s has no primary key' % table)
            else:  # the table has a primary key
                # check whether all key columns have values
                if isinstance(row, dict) and not set(keyname).issubset(row):
                    # try using the oid instead
                    if qoid and 'oid' in row:
                        keyname = ('oid',)
                    else:
                        raise KeyError(
                            'Missing value in row for specified keyname')
        if not isinstance(row, dict):
            if not isinstance(row, (tuple, list)):
                row = [row]
            if len(keyname) != len(row):
                raise KeyError(
                    'Differing number of items in keyname and row')
            row = dict(zip(keyname, row))
        params = []
        param = partial(self._prepare_param, params=params)
        col = self.escape_identifier
        what = 'oid, *' if qoid else '*'
        where = ' AND '.join('%s = %s' % (
            col(k), param(row[k], attnames[k])) for k in keyname)
        if 'oid' in row:
            if qoid:
                row[qoid] = row['oid']
            del row['oid']
        q = 'SELECT %s FROM %s WHERE %s LIMIT 1' % (
            what, self._escape_qualified_name(table), where)
        self._do_debug(q, params)
        q = self.db.query(q, params)
        res = q.dictresult()
        if not res:
            raise _db_error('No such record in %s\nwhere %s\nwith %s' % (
                table, where, self._list_params(params)))
        for n, value in res[0].items():
            if qoid and n == 'oid':
                n = qoid
            elif value is not None and attnames.get(n) == 'bytea':
                value = self.unescape_bytea(value)
            row[n] = value
        return row

    def insert(self, table, row=None, **kw):
        """Insert a row into a database table.

        This method inserts a row into a table.  The name of the table must
        be passed as the first parameter.  The other parameters are used for
        providing the data of the row that shall be inserted into the table.
        If a dictionary is supplied as the second parameter, it starts with
        that.  Otherwise it uses a blank dictionary. Either way the dictionary
        is updated from the keywords.

        The dictionary is then reloaded with the values actually inserted in
        order to pick up values modified by rules, triggers, etc.
        """
        if table.endswith('*'):  # hint for descendant tables can be ignored
            table = table[:-1].rstrip()
        if row is None:
            row = {}
        row.update(kw)
        if 'oid' in row:
            del row['oid']  # do not insert oid
        attnames = self.get_attnames(table)
        qoid = _oid_key(table) if 'oid' in attnames else None
        params = []
        param = partial(self._prepare_param, params=params)
        col = self.escape_identifier
        names, values = [], []
        for n in attnames:
            if n in row:
                names.append(col(n))
                values.append(param(row[n], attnames[n]))
        names, values = ', '.join(names), ', '.join(values)
        ret = 'oid, *' if qoid else '*'
        q = 'INSERT INTO %s (%s) VALUES (%s) RETURNING %s' % (
            self._escape_qualified_name(table), names, values, ret)
        self._do_debug(q, params)
        q = self.db.query(q, params)
        res = q.dictresult()
        if res:  # this should always be true
            for n, value in res[0].items():
                if qoid and n == 'oid':
                    n = qoid
                elif value is not None and attnames.get(n) == 'bytea':
                    value = self.unescape_bytea(value)
                row[n] = value
        return row

    def update(self, table, row=None, **kw):
        """Update an existing row in a database table.

        Similar to insert but updates an existing row.  The update is based
        on the primary key of the table or the OID value as munged by get
        or passed as keyword.

        The dictionary is then modified to reflect any changes caused by the
        update due to triggers, rules, default values, etc.
        """
        if table.endswith('*'):
            table = table[:-1].rstrip()  # need parent table name
        attnames = self.get_attnames(table)
        qoid = _oid_key(table) if 'oid' in attnames else None
        if row is None:
            row = {}
        elif 'oid' in row:
            del row['oid']  # only accept oid key from named args for safety
        row.update(kw)
        if qoid and qoid in row and 'oid' not in row:
            row['oid'] = row[qoid]
        try:  # try using the primary key
            keyname = self.pkey(table, True)
        except KeyError:  # the table has no primary key
            # try using the oid instead
            if qoid and 'oid' in row:
                keyname = ('oid',)
            else:
                raise _prg_error('Table %s has no primary key' % table)
        else:  # the table has a primary key
            # check whether all key columns have values
            if not set(keyname).issubset(row):
                # try using the oid instead
                if qoid and 'oid' in row:
                    keyname = ('oid',)
                else:
                    raise KeyError('Missing primary key in row')
        params = []
        param = partial(self._prepare_param, params=params)
        col = self.escape_identifier
        where = ' AND '.join('%s = %s' % (
            col(k), param(row[k], attnames[k])) for k in keyname)
        if 'oid' in row:
            if qoid:
                row[qoid] = row['oid']
            del row['oid']
        values = []
        keyname = set(keyname)
        for n in attnames:
            if n in row and n not in keyname:
                values.append('%s = %s' % (col(n), param(row[n], attnames[n])))
        if not values:
            return row
        values = ', '.join(values)
        ret = 'oid, *' if qoid else '*'
        q = 'UPDATE %s SET %s WHERE %s RETURNING %s' % (
            self._escape_qualified_name(table), values, where, ret)
        self._do_debug(q, params)
        q = self.db.query(q, params)
        res = q.dictresult()
        if res:  # may be empty when row does not exist
            for n, value in res[0].items():
                if qoid and n == 'oid':
                    n = qoid
                elif value is not None and attnames.get(n) == 'bytea':
                    value = self.unescape_bytea(value)
                row[n] = value
        return row

    def upsert(self, table, row=None, **kw):
        """Insert a row into a database table with conflict resolution

        This method inserts a row into a table, but instead of raising a
        ProgrammingError exception in case a row with the same primary key
        already exists, an update will be executed instead.  This will be
        performed as a single atomic operation on the database, so race
        conditions can be avoided.

        Like the insert method, the first parameter is the name of the
        table and the second parameter can be used to pass the values to
        be inserted as a dictionary.

        Unlike the insert und update statement, keyword parameters are not
        used to modify the dictionary, but to specify which columns shall
        be updated in case of a conflict, and in which way:

        A value of False or None means the column shall not be updated,
        a value of True means the column shall be updated with the value
        that has been proposed for insertion, i.e. has been passed as value
        in the dictionary.  Columns that are not specified by keywords but
        appear as keys in the dictionary are also updated like in the case
        keywords had been passed with the value True.

        So if in the case of a conflict you want to update every column that
        has been passed in the dictionary row , you would call upsert(table, row).
        If you don't want to do anything in case of a conflict, i.e. leave
        the existing row as it is, call upsert(table, row, **dict.fromkeys(row)).

        If you need more fine-grained control of what gets updated, you can
        also pass strings in the keyword parameters.  These strings will
        be used as SQL expressions for the update columns.  In these
        expressions you can refer to the value that already exists in
        the table by prefixing the column name with "included.", and to
        the value that has been proposed for insertion by prefixing the
        column name with the "excluded."

        The dictionary is modified in any case to reflect the values in
        the database after the operation has completed.

        Note: The method uses the PostgreSQL "upsert" feature which is
        only available since PostgreSQL 9.5.
        """
        if table.endswith('*'):  # hint for descendant tables can be ignored
            table = table[:-1].rstrip()
        if row is None:
            row = {}
        if 'oid' in row:
            del row['oid']  # do not insert oid
        if 'oid' in kw:
            del kw['oid']  # do not update oid
        attnames = self.get_attnames(table)
        qoid = _oid_key(table) if 'oid' in attnames else None
        params = []
        param = partial(self._prepare_param,params=params)
        col = self.escape_identifier
        names, values, updates = [], [], []
        for n in attnames:
            if n in row:
                names.append(col(n))
                values.append(param(row[n], attnames[n]))
        names, values = ', '.join(names), ', '.join(values)
        try:
            keyname = self.pkey(table, True)
        except KeyError:
            raise _prg_error('Table %s has no primary key' % table)
        target = ', '.join(col(k) for k in keyname)
        update = []
        keyname = set(keyname)
        keyname.add('oid')
        for n in attnames:
            if n not in keyname:
                value = kw.get(n, True)
                if value:
                    if not isinstance(value, basestring):
                        value = 'excluded.%s' % col(n)
                    update.append('%s = %s' % (col(n), value))
        if not values:
            return row
        do = 'update set %s' % ', '.join(update) if update else 'nothing'
        ret = 'oid, *' if qoid else '*'
        q = ('INSERT INTO %s AS included (%s) VALUES (%s)'
            ' ON CONFLICT (%s) DO %s RETURNING %s') % (
                self._escape_qualified_name(table), names, values,
                target, do, ret)
        self._do_debug(q, params)
        try:
            q = self.db.query(q, params)
        except ProgrammingError:
            if self.server_version < 90500:
                raise _prg_error(
                    'Upsert operation is not supported by PostgreSQL version')
            raise  # re-raise original error
        res = q.dictresult()
        if res:  # may be empty with "do nothing"
            for n, value in res[0].items():
                if qoid and n == 'oid':
                    n = qoid
                elif value is not None and attnames.get(n) == 'bytea':
                    value = self.unescape_bytea(value)
                row[n] = value
        else:
            self.get(table, row)
        return row

    def clear(self, table, row=None):
        """Clear all the attributes to values determined by the types.

        Numeric types are set to 0, Booleans are set to false, and everything
        else is set to the empty string.  If the row argument is present,
        it is used as the row dictionary and any entries matching attribute
        names are cleared with everything else left unchanged.
        """
        # At some point we will need a way to get defaults from a table.
        if row is None:
            row = {}  # empty if argument is not present
        attnames = self.get_attnames(table)
        for n, t in attnames.items():
            if n == 'oid':
                continue
            if t in self._num_types:
                row[n] = 0
            elif t == 'bool':
                row[n] = self._make_bool(False)
            else:
                row[n] = ''
        return row

    def delete(self, table, row=None, **kw):
        """Delete an existing row in a database table.

        This method deletes the row from a table.  It deletes based on the
        primary key of the table or the OID value as munged by get() or
        passed as keyword.

        The return value is the number of deleted rows (i.e. 0 if the row
        did not exist and 1 if the row was deleted).

        Note that if the row cannot be deleted because e.g. it is still
        referenced by another table, this method raises a ProgrammingError.
        """
        if table.endswith('*'):  # hint for descendant tables can be ignored
            table = table[:-1].rstrip()
        attnames = self.get_attnames(table)
        qoid = _oid_key(table) if 'oid' in attnames else None
        if row is None:
            row = {}
        elif 'oid' in row:
            del row['oid']  # only accept oid key from named args for safety
        row.update(kw)
        if qoid and qoid in row and 'oid' not in row:
            row['oid'] = row[qoid]
        try:  # try using the primary key
            keyname = self.pkey(table, True)
        except KeyError:  # the table has no primary key
            # try using the oid instead
            if qoid and 'oid' in row:
                keyname = ('oid',)
            else:
                raise _prg_error('Table %s has no primary key' % table)
        else:  # the table has a primary key
            # check whether all key columns have values
            if not set(keyname).issubset(row):
                # try using the oid instead
                if qoid and 'oid' in row:
                    keyname = ('oid',)
                else:
                    raise KeyError('Missing primary key in row')
        params = []
        param = partial(self._prepare_param, params=params)
        col = self.escape_identifier
        where = ' AND '.join('%s = %s' % (
            col(k), param(row[k], attnames[k])) for k in keyname)
        if 'oid' in row:
            if qoid:
                row[qoid] = row['oid']
            del row['oid']
        q = 'DELETE FROM %s WHERE %s' % (
            self._escape_qualified_name(table), where)
        self._do_debug(q, params)
        res = self.db.query(q, params)
        return int(res)

    def truncate(self, table, restart=False, cascade=False, only=False):
        """Empty a table or set of tables.

        This method quickly removes all rows from the given table or set
        of tables.  It has the same effect as an unqualified DELETE on each
        table, but since it does not actually scan the tables it is faster.
        Furthermore, it reclaims disk space immediately, rather than requiring
        a subsequent VACUUM operation. This is most useful on large tables.

        If restart is set to True, sequences owned by columns of the truncated
        table(s) are automatically restarted.  If cascade is set to True, it
        also truncates all tables that have foreign-key references to any of
        the named tables.  If the parameter only is not set to True, all the
        descendant tables (if any) will also be truncated. Optionally, a '*'
        can be specified after the table name to explicitly indicate that
        descendant tables are included.
        """
        if isinstance(table, basestring):
            only = {table: only}
            table = [table]
        elif isinstance(table, (list, tuple)):
            if isinstance(only, (list, tuple)):
                only = dict(zip(table, only))
            else:
                only = dict.fromkeys(table, only)
        elif isinstance(table, (set, frozenset)):
            only = dict.fromkeys(table, only)
        else:
            raise TypeError('The table must be a string, list or set')
        if not (restart is None or isinstance(restart, (bool, int))):
            raise TypeError('Invalid type for the restart option')
        if not (cascade is None or isinstance(cascade, (bool, int))):
            raise TypeError('Invalid type for the cascade option')
        tables = []
        for t in table:
            u = only.get(t)
            if not (u is None or isinstance(u, (bool, int))):
                raise TypeError('Invalid type for the only option')
            if t.endswith('*'):
                if u:
                    raise ValueError(
                        'Contradictory table name and only options')
                t = t[:-1].rstrip()
            t = self._escape_qualified_name(t)
            if u:
                t = 'ONLY %s' % t
            tables.append(t)
        q = ['TRUNCATE', ', '.join(tables)]
        if restart:
            q.append('RESTART IDENTITY')
        if cascade:
            q.append('CASCADE')
        q = ' '.join(q)
        self._do_debug(q)
        return self.db.query(q)

    def get_as_list(self, table, what=None, where=None,
            order=None, limit=None, offset=None, scalar=False):
        """Get a table as a list.

        This gets a convenient representation of the table as a list
        of named tuples in Python.  You only need to pass the name of
        the table (or any other SQL expression returning rows).  Note that
        by default this will return the full content of the table which
        can be huge and overflow your memory.  However, you can control
        the amount of data returned using the other optional parameters.

        The parameter 'what' can restrict the query to only return a
        subset of the table columns.  It can be a string, list or a tuple.
        The parameter 'where' can restrict the query to only return a
        subset of the table rows.  It can be a string, list or a tuple
        of SQL expressions that all need to be fulfilled.  The parameter
        'order' specifies the ordering of the rows.  It can also be a
        other string, list or a tuple.  If no ordering is specified,
        the result will be ordered by the primary key(s) or all columns
        if no primary key exists.  You can set 'order' to False if you
        don't care about the ordering.  The parameters 'limit' and 'offset'
        can be integers specifying the maximum number of rows returned
        and a number of rows skipped over.

        If you set the 'scalar' option to True, then instead of the
        named tuples you will get the first items of these tuples.
        This is useful if the result has only one column anyway.
        """
        if not table:
            raise TypeError('The table name is missing')
        if what:
            if isinstance(what, (list, tuple)):
                what = ', '.join(map(str, what))
            if order is None:
                order = what
        else:
            what = '*'
        q = ['SELECT', what, 'FROM', table]
        if where:
            if isinstance(where, (list, tuple)):
                where = ' AND '.join(map(str, where))
            q.extend(['WHERE', where])
        if order is None:
            try:
                order = self.pkey(table, True)
            except (KeyError, ProgrammingError):
                try:
                    order = list(self.get_attnames(table))
                except (KeyError, ProgrammingError):
                    pass
        if order:
            if isinstance(order, (list, tuple)):
                order = ', '.join(map(str, order))
            q.extend(['ORDER BY', order])
        if limit:
            q.append('LIMIT %d' % limit)
        if offset:
            q.append('OFFSET %d' % offset)
        q = ' '.join(q)
        self._do_debug(q)
        q = self.db.query(q)
        res = q.namedresult()
        if res and scalar:
            res = [row[0] for row in res]
        return res

    def get_as_dict(self, table, keyname=None, what=None, where=None,
            order=None, limit=None, offset=None, scalar=False):
        """Get a table as a dictionary.

        This method is similar to get_as_list(), but returns the table
        as a Python dict instead of a Python list, which can be even
        more convenient. The primary key column(s) of the table will
        be used as the keys of the dictionary, while the other column(s)
        will be the corresponding values.  The keys will be named tuples
        if the table has a composite primary key.  The rows will be also
        named tuples unless the 'scalar' option has been set to True.
        With the optional parameter 'keyname' you can specify an alternative
        set of columns to be used as the keys of the dictionary.  It must
        be set as a string, list or a tuple.

        If the Python version supports it, the dictionary will be an
        OrderedDict using the order specified with the 'order' parameter
        or the key column(s) if not specified.  You can set 'order' to False
        if you don't care about the ordering.  In this case the returned
        dictionary will be an ordinary one.
        """
        if not table:
            raise TypeError('The table name is missing')
        if not keyname:
            try:
                keyname = self.pkey(table, True)
            except (KeyError, ProgrammingError):
                raise _prg_error('Table %s has no primary key' % table)
        if isinstance(keyname, basestring):
            keyname = [keyname]
        elif not isinstance(keyname, (list, tuple)):
            raise KeyError('The keyname must be a string, list or tuple')
        if what:
            if isinstance(what, (list, tuple)):
                what = ', '.join(map(str, what))
            if order is None:
                order = what
        else:
            what = '*'
        q = ['SELECT', what, 'FROM', table]
        if where:
            if isinstance(where, (list, tuple)):
                where = ' AND '.join(map(str, where))
            q.extend(['WHERE', where])
        if order is None:
            order = keyname
        if order:
            if isinstance(order, (list, tuple)):
                order = ', '.join(map(str, order))
            q.extend(['ORDER BY', order])
        if limit:
            q.append('LIMIT %d' % limit)
        if offset:
            q.append('OFFSET %d' % offset)
        q = ' '.join(q)
        self._do_debug(q)
        q = self.db.query(q)
        res = q.getresult()
        cls = OrderedDict if order else dict
        if not res:
            return cls()
        keyset = set(keyname)
        fields = q.listfields()
        if not keyset.issubset(fields):
            raise KeyError('Missing keyname in row')
        keyind, rowind = [], []
        for i, f in enumerate(fields):
            (keyind if f in keyset else rowind).append(i)
        keytuple = len(keyind) > 1
        getkey = itemgetter(*keyind)
        keys = map(getkey, res)
        if scalar:
            rowind = rowind[:1]
            rowtuple = False
        else:
            rowtuple = len(rowind) > 1
        if scalar or rowtuple:
            getrow = itemgetter(*rowind)
        else:
            rowind = rowind[0]
            getrow = lambda row: (row[rowind],)
            rowtuple = True
        rows = map(getrow, res)
        if keytuple or rowtuple:
            namedresult = get_namedresult()
            if namedresult:
                if keytuple:
                    keys = namedresult(_MemoryQuery(keys, keyname))
                if rowtuple:
                    fields = [f for f in fields if f not in keyset]
                    rows = namedresult(_MemoryQuery(rows, fields))
        return cls(zip(keys, rows))

    def notification_handler(self,
            event, callback, arg_dict=None, timeout=None, stop_event=None):
        """Get notification handler that will run the given callback."""
        return NotificationHandler(self,
            event, callback, arg_dict, timeout, stop_event)


# if run as script, print some information

if __name__ == '__main__':
    print('PyGreSQL version' + version)
    print('')
    print(__doc__)
