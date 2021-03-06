# -*- coding: utf-8 -*-

import types
from sqlalchemy.orm.session import Session, SessionExtension
from sqlalchemy.orm.scoping import ScopedSession

__all__ = ['Sample', 'Restorable', 'DBHistory']


def _implicit_begin(session):
    if hasattr(session, 'autocommit') and not session.autocommit:
        return
    if hasattr(session, 'transactional') and session.transactional:
        return
    session.begin()


class sample_property(object):

    def __init__(self, method, name=None):
        self.method = method
        self.__doc__ = method.__doc__
        self.name = name or method.__name__

    def __get__(self, inst, cls):
        if inst is None:
            return self
        result = self.method(inst)
        #NOTE: 0.4 - save, 0.5 - save, add, 0.6 - add
        if hasattr(inst.db, 'add'):
            add = inst.db.add
        else:
            add = inst.db.save
        if isinstance(result, (list, tuple)):
            for instance in result:
                add(instance)
        else:
            add(result)
        inst.used_properties.add(self.name)
        setattr(inst, self.name, result)
        return result

    def __call__(self, obj):
        return self.method(obj)


class Sample(object):

    class __metaclass__(type):
        def __new__(cls, cls_name, bases, attributes):
            self = type.__new__(cls, cls_name, bases, attributes)
            for name in dir(self):
                if name.startswith('_') or name == 'create_all':
                    continue
                value = getattr(self, name)
                if isinstance(value, types.MethodType):
                    new_value = value.im_func
                # already decorated attribute, assigned from another class
                elif isinstance(value, sample_property) and name!= value.name:
                    new_value = value.method
                # classmethod, staticmethod and etc
                else:
                    continue
                setattr(self, name, sample_property(new_value, name=name))
            return self

    def __init__(self, db, **kwargs):
        if isinstance(db, ScopedSession):
            db = db.registry()
        self.db = db
        self.used_properties = set()
        self.__dict__.update(kwargs)

    def create_all(self):
        _implicit_begin(self.db)
        map(lambda name: getattr(self, name), dir(self))
        self.db.commit()


class _TraceNewObjectsExtension(SessionExtension):

    def __init__(self, history):
        self.history = history

    def after_flush(self, db, flush_context):
        for instance in db.new:
            identity = db.identity_key(instance=instance)
            #NOTE: version 0.4
            if len(identity) == 3:
                cls, ident, _ = identity
            #NOTE: version 0.5-0.6
            else:
                cls, ident = identity
            self.history.setdefault(cls, set()).add(ident)


class _ChainExtension(SessionExtension):

    def __init__(self, *extensions):
        self.extensions = extensions

    def __getattribute__(self, name):
        attrs = []
        for e in SessionExtension.__getattribute__(self, 'extensions'):
            attrs.append(getattr(e, name, None))
        attrs = filter(None, attrs)
        if not attrs:
            raise AttributeError(name)
        def wrapper(*args, **kwargs):
            for func in attrs:
                func(*args, **kwargs)
        wrapper.func_name = name
        return wrapper


def _append_extension(session, extension):
    #NOTE: version 0.4
    if hasattr(session, 'extension'):
        old_extension = session.extension
        new_extension = _ChainExtension(old_extension, extension)
    #NOTE: version 0.5-0.6
    elif hasattr(session, 'extensions'):
        old_extension = session.extensions
        new_extension = old_extension + [extension]
    else:
        raise ValueError('Object %r has no attrs like '
                         '`extension` or`extensions`' % session)
    return old_extension, new_extension


class Restorable(object):

    def __init__(self, db, watch=None):
        if isinstance(db, ScopedSession):
            db = db.registry()
        self.db = db
        self.watch = watch or db
        self.history = history = {}
        extension = _TraceNewObjectsExtension(history)
        self.old_extension, self.extension = _append_extension(self.watch,
                                                                extension)

    def __enter__(self):
        if hasattr(self.watch, 'extension'):
            self.watch.extension = self.extension
        else:
            self.watch.extensions = self.extension

    def __exit__(self, type, value, traceback):
        db = self.db
        db.rollback()
        #NOTE: version 0.4
        if hasattr(db, 'clear'):
            db.clear()
        #NOTE: version 0.5-0.6
        else:
            db.expunge_all()
        old_autoflush = db.autoflush
        db.autoflush = False
        _implicit_begin(db)
        for cls, ident_set in self.history.items():
            for ident in ident_set:
                instance = db.query(cls).get(ident)
                if instance is not None:
                    db.delete(instance)
        db.commit()
        db.close()
        db.autoflush = old_autoflush
        if hasattr(self.watch, 'extension'):
            self.watch.extension = self.old_extension
        else:
            self.watch.extensions = self.old_extension


class _HistoryExtension(SessionExtension):

    def __init__(self, obj):
        self.obj = obj

    def after_flush(self, db, flush_context):
        history = self.obj
        def identityset_to_set(obj):
            return set(obj._members.values())
        def populate_idents_dict(idents, objects):
            for obj in objects:
                ident = db.identity_key(instance=obj)
                idents.setdefault(ident[0], set()).add(ident[1])
        history.created = history.created.union(identityset_to_set(db.new))
        history.updated = history.updated.union(identityset_to_set(db.dirty))
        history.deleted = history.deleted.union(identityset_to_set(db.deleted))
        populate_idents_dict(history.created_idents, history.created)
        populate_idents_dict(history.updated_idents, history.updated)
        populate_idents_dict(history.deleted_idents, history.deleted)


class DBHistory(object):

    def __init__(self, session):
        assert isinstance(session, (Session, ScopedSession))
        self.session = session
        #XXX: It is not clear do we need events on class or object
        self._target = session
        if isinstance(session, ScopedSession):
            self._target = session.registry()
        self.created = set()
        self.deleted = set()
        self.updated = set()
        self.created_idents = {}
        self.updated_idents = {}
        self.deleted_idents = {}
        extension = _HistoryExtension(self)

        self.old_extension, self.extension = _append_extension(self._target,
                                                               extension)
    def last(self, model_cls, mode):
        assert mode in ('created', 'updated', 'deleted')
        if mode == 'deleted':
            # Because there is not data in DB we return detached object set.
            return set([inst for inst in self.deleted \
                        if isinstance(inst, model_cls)])
        idents = getattr(self, '%s_idents' % mode).get(model_cls, set())
        return set([self.session.query(model_cls).get(ident) \
                    for ident in idents])

    def last_created(self, model_cls):
        return self.last(model_cls, 'created')

    def last_updated(self, model_cls):
        return self.last(model_cls, 'updated')

    def last_deleted(self, model_cls):
        return self.last(model_cls, 'deleted')

    def assert_(self, model_cls, ident=None, mode='created'):
        dataset = self.last(model_cls, mode)
        error_msg = 'No instances of %s were %s' % (model_cls, mode)
        assert dataset, error_msg
        if ident is not None:
            ident = ident if isinstance(ident, (tuple, list)) else (ident,)
            item = [i for i in dataset \
                    if self._target.identity_key(instance=i)[1] == ident]
            assert item,'No insatances of %s with identity %r were %s' % \
                   (model_cls, ident, mode)
            return item[0]
        return dataset

    def assert_created(self, model_cls, ident=None):
        return self.assert_(model_cls, ident, 'created')

    def assert_updated(self, model_cls, ident=None):
        return self.assert_(model_cls, ident, 'updated')

    def assert_deleted(self, model_cls, ident=None):
        return self.assert_(model_cls, ident, 'deleted')

    def assert_one(self, dataset, model_cls, mode):
        if len(dataset) != 1:
            raise AssertionError('%d instance(s) of %s %s, '
                                 'need only one' % (len(dataset),
                                                    model_cls,
                                                    mode))
        return dataset.pop()

    def assert_created_one(self, model_cls):
        result = self.assert_created(model_cls)
        return self.assert_one(result, model_cls, 'created')

    def assert_deleted_one(self, model_cls):
        result = self.assert_deleted(model_cls)
        return self.assert_one(result, model_cls, 'deleted')

    def assert_updated_one(self, model_cls):
        result = self.assert_updated(model_cls)
        return self.assert_one(result, model_cls, 'updated')

    def clear(self):
        self.created = set()
        self.deleted = set()
        self.updated = set()
        self.created_idents = {}
        self.updated_idents = {}
        self.deleted_idents = {}

    def __enter__(self):
        if hasattr(self._target, 'extension'):
            self._target.extension = self.extension
        else:
            self._target.extensions = self.extension
        return self

    def __exit__(self, type, value, traceback):
        if hasattr(self._target, 'extension'):
            self._target.extension = self.old_extension
        else:
            self._target.extensions = self.old_extension
