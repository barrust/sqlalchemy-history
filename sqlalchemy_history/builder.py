"""Builder Module Detects and builds version class for models and version tables collected during instrument
 phase by the manager
"""

from copy import copy
from inspect import getmro
from functools import wraps

import sqlalchemy as sa
from sqlalchemy.orm.descriptor_props import ConcreteInheritedProperty
from sqlalchemy_utils.functions import get_declarative_base, get_hybrid_properties
from sqlalchemy_history.utils import get_association_proxies, version_class

from sqlalchemy_history.model_builder import ModelBuilder
from sqlalchemy_history.relationship_builder import RelationshipBuilder
from sqlalchemy_history.table_builder import TableBuilder


def prevent_reentry(handler):
    in_handler = False

    @wraps(handler)
    def check_reentry(*args, **kwargs):
        nonlocal in_handler
        if in_handler:
            return
        in_handler = True
        handler(*args, **kwargs)
        in_handler = False

    return check_reentry


class Builder(object):
    def build_tables(self):
        """
        Build tables for version models based on classes that were collected
        during class instrumentation process.

        """
        for cls in self.manager.pending_classes:
            if not self.manager.option(cls, "versioning"):
                continue

            if self.manager.option(cls, "create_tables"):
                inherited_table = None
                for class_ in self.manager.tables:
                    if issubclass(cls, class_) and cls.__table__ == class_.__table__:
                        inherited_table = self.manager.tables[class_]
                        break

                builder = TableBuilder(self.manager, cls.__table__, model=cls)
                if inherited_table is not None:
                    self.manager.tables[class_] = builder(inherited_table)
                else:
                    table = builder()
                    self.manager.tables[cls] = table

    def closest_matching_table(self, model):
        """
        Returns the closest matching table from the generated tables dictionary
        for given model. First tries to fetch an exact match for given model.
        If no table was found then tries to match given model as a subclass.

        :param model: SQLAlchemy declarative model class.

        """
        if model in self.manager.tables:
            return self.manager.tables[model]
        subclasses = [cls for cls in self.manager.tables if issubclass(model, cls)]
        ordered_subclasses = [cls for cls in getmro(model) if cls in subclasses]
        return self.manager.tables[ordered_subclasses[0]] if ordered_subclasses else None

    def build_models(self):
        """
        Build declarative version models based on classes that were collected
        during class instrumentation process.


        """
        if self.manager.pending_classes:
            for cls in self.manager.pending_classes:
                if not self.manager.option(cls, "versioning"):
                    continue

                table = self.closest_matching_table(cls)
                if table is not None:
                    builder = ModelBuilder(self.manager, cls)
                    version_cls = builder(table, self.manager.transaction_cls)

                    self.manager.plugins.after_version_class_built(cls, version_cls)

            self.manager.plugins.after_build_models(self.manager)

    def build_relationships(self, version_classes):
        """
        Builds relationships for all version classes.

        :param version_classes: list of generated version classes

        """
        for cls in version_classes:
            if not self.manager.option(cls, "versioning"):
                continue

            for prop in sa.inspect(cls).iterate_properties:
                if prop.key == "versions":
                    continue
                builder = RelationshipBuilder(self.manager, cls, prop)
                builder()

    def instrument_versioned_classes(self, mapper, cls):
        """
        Collect versioned class and add it to pending_classes list.

        :param mapper: SQLAlchemy mapper object
        :param cls: SQLAlchemy declarative class

        """
        if not self.manager.options["versioning"]:
            return

        if hasattr(cls, "__versioned__"):
            if not cls.__versioned__.get("class") and cls not in self.manager.pending_classes:
                self.manager.pending_classes.append(cls)
                self.manager.metadata = cls.metadata

    def build_transaction_class(self):
        if self.manager.pending_classes:
            cls = self.manager.pending_classes[0]
            self.manager.declarative_base = get_declarative_base(cls)
            self.manager.create_transaction_model()
            self.manager.plugins.after_build_tx_class(self.manager)

    @prevent_reentry
    def configure_versioned_classes(self):
        """
        Configures all versioned classes that were collected during
        instrumentation process. The configuration has 6 steps:

        1. Build tables for version models.
        2. Build the actual version model declarative classes.
        3. Build relationships between these models.
        4. Empty pending_classes list so that consecutive mapper configuration
           does not create multiple version classes
        5. Build aliases for columns.
        6. Assign all versioned attributes to use active history.
        7. Add Association proxy for Versioned Models.
        8. Add Hybrid Property to Versioned Model
        """
        if not self.manager.options["versioning"]:
            return

        self.build_tables()
        self.build_transaction_class()

        if not self.manager.options["create_models"]:
            self.manager.pending_classes = []
            return

        self.build_models()

        # Create copy of all pending versioned classes so that we can inspect
        # them later when creating relationships.
        pending_classes_copies = copy(self.manager.pending_classes)
        self.manager.pending_classes = []
        self.build_relationships(pending_classes_copies)
        self.enable_active_history(pending_classes_copies)
        self.create_column_aliases(pending_classes_copies)
        self.create_association_proxies(pending_classes_copies)
        self.create_hybrid_properties(pending_classes_copies)

    def enable_active_history(self, version_classes):
        """
        Assign all versioned attributes to use active history.

        :param version_classes: All version classes tracked.

        """
        for cls in version_classes:
            for prop in sa.inspect(cls).iterate_properties:
                if isinstance(prop, ConcreteInheritedProperty):
                    continue
                attr = getattr(cls, prop.key)
                attr.impl.active_history = True

    def create_column_aliases(self, version_classes):
        """
        Create aliases for the columns from the original model.
        This, for example, imitates the behavior of @declared_attr columns.

        :param version_classes:  All version classes tracked.

        """
        for cls in version_classes:
            model_mapper = sa.inspect(cls)
            version_class = self.manager.version_class_map.get(cls)
            if not version_class:
                continue

            version_class_mapper = sa.inspect(version_class)

            for key, column in model_mapper.columns.items():
                if key != column.key:
                    version_class_column = version_class.__table__.c.get(column.key)

                    if version_class_column is None:
                        continue

                    version_class_mapper.add_property(key, sa.orm.column_property(version_class_column))

    def create_association_proxies(self, version_classes):
        """
        Create Association proxy for Column of Versioned Models from Original Model
        """
        for cls in version_classes:
            assoc_prox_maps = get_association_proxies(cls)
            for key, prop in assoc_prox_maps.items():
                versioned_target_class = version_class(cls)
                setattr(
                    versioned_target_class,
                    key,
                    property(fget=prop.__get__),
                )

    def create_hybrid_properties(self, version_classes):
        """
        Create Hybrid Property for Column as a property in Versioned Models from Original Model
        """
        for cls_ in version_classes:
            hybrid_property_maps = get_hybrid_properties(cls_)
            for key, prop in hybrid_property_maps.items():
                versioned_target_class = version_class(cls_)
                setattr(
                    versioned_target_class,
                    key,
                    sa.ext.hybrid.hybrid_property(fget=prop.fget),
                )
