NEON_APPS = {'grader'}


class GraderRouter:
    def db_for_read(self, model, **hints):
        if model._meta.app_label in NEON_APPS:
            return 'neon'
        return 'default'

    def db_for_write(self, model, **hints):
        if model._meta.app_label in NEON_APPS:
            return 'neon'
        return 'default'

    def allow_relation(self, obj1, obj2, **hints):
        db1 = 'neon' if obj1._meta.app_label in NEON_APPS else 'default'
        db2 = 'neon' if obj2._meta.app_label in NEON_APPS else 'default'
        return db1 == db2

    def allow_migrate(self, db, app_label, model_name=None, **hints):
        if app_label in NEON_APPS:
            return db == 'neon'
        return db == 'default'
