from contextlib import contextmanager
from contextvars import ContextVar
from django.core.exceptions import ImproperlyConfigured

_current_database = ContextVar("gym_database", default=None)


@contextmanager
def gym_database(alias):
    if not alias.startswith("gym_"):
        raise ImproperlyConfigured("A gym database alias is required")
    token = _current_database.set(alias)
    try:
        yield
    finally:
        _current_database.reset(token)


class GymDatabaseRouter:
    def db_for_read(self, model, **hints):
        if model._meta.app_label == "tenant_data":
            alias = _current_database.get()
            if alias is None:
                raise ImproperlyConfigured("Select an authorized gym database first")
            return alias
        return "default"

    db_for_write = db_for_read

    def allow_relation(self, obj1, obj2, **hints):
        return obj1._state.db == obj2._state.db

    def allow_migrate(self, db, app_label, **hints):
        if app_label == "tenant_data":
            return db.startswith("gym_")
        return db == "default"
