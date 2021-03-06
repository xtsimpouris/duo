# -*- coding: utf-8 -*-

"""\
duo -- a powerful, dynamic, pythonic interface to AWS DynamoDB
==============================================================

Welcome to duo
--------------

Glad to see someone reading the source. I'll try to keep the
experience non-threatening.

Duo has two jobs:

Make working with DynamoDB more declarative
^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^

To this end, duo provides some special metaclass magic to hook up your
declared table and item subclasses. It also gives you
`getattr()`-style descriptor fields that serve as lightweight schema
*and* self-documenting code.

Make working with DynamoDB's limited data-types less painful
^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^

You've got two data types (well, yes, and sets, but let's ignore those
for now). Obviously, you'll want to be serializing things. Duo's
fields do the heavy lifting for you for some basic extended types, and
it's easy to write your own fields.

Got all that? Read on.
"""
import warnings
import collections
import datetime
import time
import json
import hashlib

import boto
from boto.dynamodb2.items       import Item as _Item
from boto.dynamodb2.exceptions  import ItemNotFound
from boto.dynamodb2.table       import Table as _Table

# First off, since we have integers as one of our two native data
# types, we're going to do enumerated types, which are great. You're
# going to love these, or possibly hate them.

# We're also going to use a metaclass. You don't have to understand
# how metaclasses work to use this, but I hope this may shed some
# light on what they are and how they work.


class EnumMeta(type):
    """Simple metaclass for enumerated types.

    To create a new enumerated type, set this metaclass on a new
    class, then subclass that to create new members of the enumerated
    type.

    Example::

        class Access(object):
            __metaclass__ = duo.EnumMeta


        ### WARNING: Order of definition matters! Add new access types to the end.
        # NO_ACCESS.index == 0
        class NO_ACCESS(Access): pass


        # FULL_ACCESS.index == 1
        class FULL_ACCESS(Access): pass


        # TRIAL_ACCESS.index == 2
        class TRIAL_ACCESS(Access): pass

        ### Here's what that gets us:
        Access.NO_ACCESS == NO_ACCESS
        Access[0] == NO_ACCESS
        int(NO_ACCESS) == 0
        str(NO_ACCESS) == 'NO_ACCESS'
        # Yes, that's right: magic __ methods on classes.

    """
    def __init__(cls, name, bases, attrs):
        # Based on Marty Alchin's simple plugin framework idea:
        # http://martyalchin.com/2008/jan/10/simple-plugin-framework/
        if not hasattr(cls, 'members'):
            # This branch only executes when processing the mount point itself.
            # So, since this is a new plugin type, not an implementation, this
            # class shouldn't be registered as a plugin. Instead, it sets up a
            # list where plugins can be registered later.
            cls.members = []
        else:
            # This must be a plugin implementation, which should be registered.
            # Simply appending it to the list is all that's needed to keep
            # track of it later.
            cls.members.append(cls)
            cls.index = len(cls.members) - 1
            setattr(cls.__class__, cls.__name__, cls)
            cls.key = cls.__name__

    ### MAGIC METHODS: These magic methods are on the *type*, and
    ### because a class is an instantiation of a type, they're usable
    ### on classes of this type. Yes, that's right, you can cast your
    ### subclass to an integer.
    def __iter__(cls):
        return iter(cls.members)

    def __len__(cls):
        return len(cls.members)

    def __getitem__(cls, idx):
        try:
            if isinstance(idx, EnumMeta):
                return cls.members[int(idx)]
            elif isinstance(idx, int):
                return cls.members[idx]
            elif isinstance(idx, basestring):
                try:
                    return getattr(cls, idx)
                except AttributeError:
                    pass
        except KeyError:
            raise TypeError("'%s' does not support indexing." % cls.__name__)

        # Failing all that, raise our own KeyError.
        raise KeyError(idx)

    def __int__(cls):
        try:
            return cls.index
        except AttributeError:
            raise ValueError("'%s' does not support integer casting.")

    def __nonzero__(cls):
        return bool(int(cls))

    def __cmp__(self, other):
        if isinstance(other, basestring):
            return cmp(str(self), other)
        else:
            return cmp(int(self), other)

    def __str__(cls):
        try:
            return cls.key
        except AttributeError:
            return super(EnumMeta, cls).__str__()

    def __unicode__(cls):
        try:
            return unicode(cls.key)
        except AttributeError:
            return super(EnumMeta, cls).__unicode__()


# Now we're getting to the meat of the DynamoDB interactions. First
# off, we need a way to manage an AWS connection to DynamoDB, and
# associate a custom table type with that connection.


class DynamoDB(object):
    """Manages a connection to DynamoDB and looks up custom Table handlers.

    Example::

        DYNAMODB = duo.DynamoDB(
            key = getattr(os.environ.get('DYNAMODB_ACCESS_KEY_ID'), ''),
            secret = getattr(os.environ.get('DYNAMODB_SECRET_ACCESS_KEY'), ''),
            cache = cache  # pymemcached-compatible cache object.
            )

         # Assuming you've already declared a table named `my_table_name`:
         my_table = DYNAMODB['my_table_name']
    """
    def __init__(self, key, secret, cache=None):
        self.key = key
        self.secret = secret
        self._tables = {}
        self.cache = cache

    @property
    def connection(self):
        """Lazy-load a boto DynamoDB connection.
        """
        if not hasattr(self, '_connection'):
            self._connection = boto.connect_dynamodb(
                aws_access_key_id=self.key,
                aws_secret_access_key=self.secret
                )
        return self._connection

    def reset(self):
        """Reset the DynamoDB connection and clear any cached tables.
        """
        if hasattr(self, '_connection'):
            del self._connection
        self._tables.clear()

    def __getitem__(self, key):
        """Retrieve a registered custom table by name.
        """
        if isinstance(key, tuple):
            table_name, table_model = key
        else:
            table_name = key
            table_model = None

        if hasattr(table_name, 'table_name'):
            table_name = table_name.table_name

        if table_name not in self._tables:
            self._tables[table_name] = _Table(table_name)

        if table_model:
            table = table_model(self, self._tables[table_name], cache=self.cache)
        else:
            table = Table._table_types[table_name](self, self._tables[table_name], cache=self.cache)
        table.table_name = table_name
        table.connection = self.connection
        return table


# Another metaclass. This one's similar to the EnumMeta, but much
# simpler: it's just a place to record subclasses of our Table and
# Item mount-points.


class _TableMeta(type):
    """Metaclass plugin mount for plugins related to AWS DynamoDB tables.

    Don't worry about using this one yourself. This is the magic that
    glues your custom tables and your custom items together.
    """
    def __init__(cls, name, bases, attrs):
        # Marty Alchin's simple plugin framework, again:
        # http://martyalchin.com/2008/jan/10/simple-plugin-framework/
        if not hasattr(cls, '_table_types'):
            # This branch only executes when processing the mount point itself.
            # So, since this is a new plugin mount, not an implementation, this
            # class shouldn't be registered as a plugin. Instead, it sets up a
            # registry where custom plugins can be registered later.
            cls._table_types = collections.defaultdict(lambda: cls)
        else:
            # This must be a plugin implementation, which should be registered.
            cls._table_types[cls.table_name] = cls

            # Special handling for class member fields, if there are
            # any. A field needs to know what its name is.
            for name, value in attrs.copy().iteritems():
                if isinstance(value, Field):
                    value.name = name


class Item(_Item):
    """A boto DynamoDB Item, with caching secret sauce.

    Subclass to customize fields and caching behavior. Subclassing
    auto-registers with the DB.
    """
    # This is the mount-point for custom Items. Sub-classes will
    # register themselves with this mount-point.
    __metaclass__ = _TableMeta

    duo_db = None
    duo_table = None

    cache = None
    cache_duration = None
    is_new = False

    def __init__(self, *args, **kwargs):
        super(Item, self).__init__(*args, **kwargs)

    def pop(self, key, default):
        """Pops a value from the dict, and returns it
        """
        if key in self:
            ret = self[key]
            del self[key]
        else:
            ret = default

        return ret

    @property
    def dynamo_key(self):
        """Return the hash_key or (hash_key, range_key) key.

        The returned value is suitable for looking up the item in the
        table via __getitem__(key)
        """
        if self.range_key_name is None:
            return self.hash_key
        else:
            return (self.hash_key, self.range_key)

    @property
    def _cache_key(self):
        """Determine the key for accessing the item in the cache.
        """
        return self.duo_table._get_cache_key(self.hash_key, self.range_key)

    def _set_cache(self):
        """Store the item in the cache.
        """
        if self.cache is not None and self.cache_duration is not None:
            table = self.duo_table
            key = table._get_cache_key(self[table.hash_key_name], self.get(table.range_key_name, None))
            duration = self.cache_duration if self.cache_duration is not None else table.cache_duration
            self.cache.set(key, self.items(), duration)

    def _delete_cache(self):
        """Remove the item from the cache.
        """
        if self.cache is not None:
            table = self.duo_table
            key = table._get_cache_key(self[table.hash_key_name], self.get(table.range_key_name, None))
            self.cache.delete(key)

    def put(self, *args, **kwargs):
        """Put the item in the database, and also in the cache.
        """
        result = super(Item, self).save(*args, **kwargs)
        if not result:
            # Den petixe i apothikefsi, i brethike allo peiragmeno item apo katw
            # Gia ipoxrewtikki antikatastasi overwrite=True
            return False
        self.is_new = False
        try:
            self._set_cache()
        except Exception as e:
            warnings.warn('Cache write-through failed on put(). %s: %s' % (e.__class__.__name__, e.message))
        return result

    def delete(self, *args, **kwargs):
        """Delete the item from the database, and also from the cache.
        """
        result = super(Item, self).delete(*args, **kwargs)
        self.is_new = True
        try:
            self._delete_cache()
        except Exception as e:
            warnings.warn('Cache write-through failed on delete(). %s: %s' % (e.__class__.__name__, e.message))
        return result


class Table(object):
    """A DynamoDB Table, with super dict-like powers.

    Subclass to customize behavior. Subclassing auto-registers with
    the DB.
    """
    # This is the mount-point for custom Tables. Sub-classes will
    # register themselves with this mount-point.
    __metaclass__ = _TableMeta

    table_name = None
    hash_key_name = None
    range_key_name = None

    cache = None
    cache_prefix = None

    def __init__(self, db, table, cache=None):
        self.duo_db = db
        self.table = table
        if self.cache is None:
            self.cache = cache
        super(Table, self).__init__()

    def keys(self):
        """Return an iterator of object keys, either by `hash_key` or `(hash_key, range_key)`.

        WARNING: This performs a table scan, which can be expensive on a large table.
        """
        if self.range_key_name is None:
            return (i[self.hash_key_name] for i in self.scan(attributes_to_get=[self.hash_key_name]))
        else:
            return ((i[self.hash_key_name], i[self.range_key_name])
                    for i in self.scan(attributes_to_get=[self.hash_key_name, self.range_key_name]))

    def items(self):
        """Return an iterator of object key/value pairs, either by `hash_key` or `(hash_key, range_key)`.

        WARNING: This performs a table scan, which can be expensive on a large table.
        """
        if self.range_key_name is None:
            return ((i[self.hash_key_name], i) for i in self.scan())
        else:
            return (((i[self.hash_key_name], i[self.range_key_name]), i)
                    for i in self.scan())

    def values(self):
        """Return an iterator of objects in the table.

        Equivalent of `.scan()` sans arguments.

        WARNING: This performs a table scan, which can be expensive on a large table.
        """
        return self.scan()

    def create(self, hash_key, range_key=None, **kwargs):
        """Create an item given the specified attributes.
        """
        data = kwargs
        data[self.hash_key_name] = hash_key
        if self.range_key_name and range_key:
            data[self.range_key_name] = range_key
        # item = _Item(self.table, data = data)
        return self._extend(Item._table_types[self.table_name](self.table, data = data), is_new=True)

        """
        item = self.table.new_item(
            hash_key = hash_key,
            range_key = range_key,
            attrs = kwargs,
            item_class = Item._table_types[self.table_name],
            )
        return self._extend(item, is_new=True)
        """

    def _extend(self, item, is_new=False):
        """Extend the given Item with some necessary attributes.
        """
        item.is_new = is_new
        item.cache = self.cache
        item.duo_table = self
        item.duo_db = self.duo_db
        item.hash_key_name = self.hash_key_name
        item.range_key_name = self.range_key_name
        return item

    def _extend_iter(self, items, is_new=False):
        """Extend a collection of Items with some necessary attributes.
        """
        for item in items:
            yield self._extend(item, is_new)

    @classmethod
    def _get_cache_key(cls, hash_key, range_key):
        """Determine the cache key for a given table key.

        Specify `range_key=None` for a hash-only key.
        """
        if range_key is None:
            key = '%s_%s' % (cls.cache_prefix or cls.table_name, hash_key)
        else:
            key = '%s_%s_%s' % (cls.cache_prefix or cls.table_name, hash_key, range_key)
        return hashlib.sha224(key).hexdigest()

    def _get_cache(self, hash_key, range_key=None):
        """Retrieve the specified item from the cache, if available.
        """
        if self.cache is None:
            return None
        else:
            key = self._get_cache_key(hash_key, range_key)
            cached = self.cache.get(key)
            if cached is not None:
                # Build an Item.

                data = dict(cached)
                cached = self._extend(Item._table_types[self.table_name](self.table, data = data, loaded = True))
            return cached


    def get_item(self, hash_key, range_key=None, consistent=False, attributes=None, **params):
        data = {}
        data[self.hash_key_name] = hash_key
        if self.range_key_name and range_key:
            data[self.range_key_name] = range_key

        raw_key = self.table._encode_keys(data)
        item_data = self.table.connection.get_item(
            self.table_name,
            raw_key,
            attributes_to_get=attributes,
            consistent_read=consistent
        )
        if 'Item' not in item_data:
            raise ItemNotFound("Item (%s, %s) couldn't be found." % (hash_key, range_key))
        item = self._extend(Item._table_types[self.table_name](self.table))
        item.load(item_data)
        item._set_cache()
        return item        

    def __getitem__(self, key):
        if isinstance(key, tuple):
            hash_key, range_key = key
        else:
            hash_key = key
            range_key = None

        # Check the cache first.
        cached = self._get_cache(hash_key, range_key)
        if cached is not None:
            return cached

        try:
            if range_key is None:
                if self.range_key_name is None:
                    item = self.get_item(hash_key)
                else:
                    return self.query(hash_key)
            else:
                item = self.get_item(hash_key, range_key)
        except ItemNotFound:
            item = self.create(hash_key, range_key)

        return item

    def query(self, limit=None, index=None, reverse=False, consistent=False, attributes=None,
                max_page_size=None, query_filter=None, conditional_operator=None, **filter_kwargs):
        """Perform a query on the table.

        Returns items using the registered subclass, if one has been registered.

        See http://boto.readthedocs.org/en/latest/ref/dynamodb.html#boto.dynamodb.table.Table.query
        """
        return self.table.query_2(
            limit                 = limit,
            index                 = index,
            reverse               = reverse,
            consistent            = consistent,
            attributes            = attributes,
            max_page_size         = max_page_size,
            query_filter          = query_filter,
            conditional_operator  = conditional_operator,
            **filter_kwargs
          )

    def scan(self, **kwargs):
        """Scan through this table.

        This is a very long and expensive operation, and should be avoided if at all possible.

        Returns items using the registered subclass, if one has been registered.

        See http://boto.readthedocs.org/en/latest/ref/dynamodb.html#boto.dynamodb.table.Table.scan
        """
        return self.table.scan(**kwargs)


class NONE(object): pass


class Field(object):
    """A Field acts as a data descriptor on Item subclasses.
    """
    name = None

    def __init__(self, default=NONE, readonly=False):
        self.default = default
        self.readonly = readonly
        super(Field, self).__init__()

    def to_python(self, obj, value):
        raise NotImplementedError()

    def from_python(self, obj, value):
        raise NotImplementedError()

    def __get__(self, obj, type=None):
        try:
            value = self.to_python(obj, obj[self.name])
        except KeyError:
            if self.default is not NONE:
                if callable(self.default) and not isinstance(self.default, EnumMeta):
                    value = self.default(obj)
                else:
                    value = self.default
                value = self.to_python(obj, value)
                if value:
                    # Populate the default on the object.
                    setattr(obj, self.name, value)
            else:
                return None

        return value

    def __set__(self, obj, value):
        if self.name == getattr(obj, 'hash_key_name'):
            raise AttributeError('Cannot set hash key `%s`!' % self.name)
        elif self.name == getattr(obj, 'range_key_name'):
            raise AttributeError('Cannot set range key `%s`!' % self.name)
        elif self.readonly:
            raise AttributeError('`%s` is read-only!' % self.name)
        else:
            if value is None:
                # If value is None and the attribute exists, clear it.
                if self.name in obj:
                    del obj[self.name]
            else:
                obj[self.name] = self.from_python(obj, value)

    def __delete__(self, obj):
        if self.name == getattr(obj, 'hash_key_name'):
            raise AttributeError('Cannot delete hash key `%s`!' % self.name)
        elif self.name == getattr(obj, 'range_key_name'):
            raise AttributeError('Cannot delete range key `%s`!' % self.name)
        elif self.readonly:
            raise AttributeError('`%s` is read-only!' % self.name)
        else:
            del obj[self.name]


class UnicodeField(Field):
    """Store a simple unicode string as a native DynamoDB string.
    """
    def to_python(self, obj, value):
        return value

    def from_python(self, obj, value):
        return unicode(value)


class IntegerField(Field):
    """Store a simple integer as a native DynamoDB integer.
    """
    def to_python(self, obj, value):
        return value

    def from_python(self, obj, value):
        return int(value)


IntField = IntegerField


class _ChoiceMixin(Field):
    """A field mixin that enforces a set of possible values, using an Enum.
    """
    def __init__(self, **kwargs):
        self.enum_type = kwargs.pop('enum_type')
        super(_ChoiceMixin, self).__init__(**kwargs)

    def to_python(self, obj, value):
        return self.enum_type[value]


class ChoiceField(_ChoiceMixin, UnicodeField):
    """A unicode field that enforces a set of possible values, using an Enum.
    """
    def from_python(self, obj, value):
        return unicode(self.enum_type[value])


class EnumField(_ChoiceMixin, IntField):
    """An integer field that enforces a set of possible values, using an Enum.
    """
    def from_python(self, obj, value):
        return int(self.enum_type[value])


class DateField(Field):
    """An integer field that stores `datetime.date` objects as ordinal integers.
    """
    def to_python(self, obj, value):
        if value is None or value == 0:
            return None

        return datetime.date.fromordinal(value)

    def from_python(self, obj, value):
        if value is None or value == 0:
            return 0

        try:
            return value.toordinal()
        except AttributeError:
            raise ValueError('DateField requires a `datetime.date` object.')


class DateTimeField(Field):
    """An integer field that stores `datetime.datedatetime` objects as unix timestamps.
    """
    def to_python(self, obj, value):
        if value is None or value == 0:
            return None

        return datetime.datetime.fromtimestamp(value)

    def from_python(self, obj, value):
        if value is None or value == 0:
            return 0

        try:
            return time.mktime(value.timetuple())
        except AttributeError:
            raise ValueError('DateTimeField requires a `datetime.datetime` object.')


class ForeignKeyField(Field):
    """A unicode field that stores foreign DynamoDB table references as a JSON-serialized string.
    """
    def to_python(self, obj, value):
        if isinstance(value, Item):
            return value

        elif isinstance(value, dict):
            fk_dict = value
        else:
            fk_dict = json.loads(value)
        table_name = fk_dict['table']
        key = fk_dict['key']
        if isinstance(key, list):
            key = tuple(key)
        table = obj.duo_db[table_name]
        return table[key]

    def from_python(self, obj, value):
        return json.dumps({
            'table': value.table_name,
            'key': value.dynamo_key
            })
