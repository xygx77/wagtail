import collections

from django import forms
from django.core.exceptions import ValidationError
from django.forms.utils import ErrorList
from django.template.loader import render_to_string
from django.utils.functional import cached_property
from django.utils.html import format_html, format_html_join
from django.utils.safestring import mark_safe

from wagtail.admin.staticfiles import versioned_static
from wagtail.admin.telepath import Adapter, register

from .base import (
    Block,
    BoundBlock,
    DeclarativeSubBlocksMetaclass,
    get_error_json_data,
    get_error_list_json_data,
    get_help_icon,
)

__all__ = [
    "BaseStructBlock",
    "StructBlock",
    "StructValue",
    "StructBlockValidationError",
]


class StructBlockValidationError(ValidationError):
    def __init__(self, block_errors=None, non_block_errors=None):
        # non_block_errors may be passed here as an ErrorList, a plain list (of strings or
        # ValidationErrors), or None.
        # Normalise it to be an ErrorList, which provides an as_data() method that consistently
        # returns a flat list of ValidationError objects.
        self.non_block_errors = ErrorList(non_block_errors)

        # block_errors may be passed here as None, or a dict keyed by the names of the child blocks
        # with errors.
        # Items in this list / dict may be:
        #  - a ValidationError instance (potentially a subclass such as StructBlockValidationError)
        #  - an ErrorList containing a single ValidationError
        #  - a plain list containing a single ValidationError
        # All representations will be normalised to a dict of ValidationError instances,
        # which is also the preferred format for the original argument to be in.
        self.block_errors = {}
        if block_errors is None:
            pass
        else:
            for name, val in block_errors.items():
                if isinstance(val, ErrorList):
                    self.block_errors[name] = val.as_data()[0]
                elif isinstance(val, list):
                    self.block_errors[name] = val[0]
                else:
                    self.block_errors[name] = val

        super().__init__("Validation error in StructBlock")

    def as_json_data(self):
        result = {}
        if self.non_block_errors:
            result["messages"] = get_error_list_json_data(self.non_block_errors)
        if self.block_errors:
            result["blockErrors"] = {
                name: get_error_json_data(error)
                for (name, error) in self.block_errors.items()
            }
        return result


class StructValue(collections.OrderedDict):
    """A class that generates a StructBlock value from provided sub-blocks"""

    def __init__(self, block, *args):
        super().__init__(*args)
        self.block = block

    def __html__(self):
        return self.block.render(self)

    def render_as_block(self, context=None):
        return self.block.render(self, context=context)

    @cached_property
    def bound_blocks(self):
        return collections.OrderedDict(
            [
                (name, block.bind(self.get(name)))
                for name, block in self.block.child_blocks.items()
            ]
        )

    def __reduce__(self):
        return (self.__class__, (self.block,), None, None, iter(self.items()))


class PlaceholderBoundBlock(BoundBlock):
    """
    Provides a render_form method that outputs a block placeholder, for use in custom form_templates
    """

    def render_form(self):
        return format_html('<div data-structblock-child="{}"></div>', self.block.name)


class BaseStructBlock(Block):
    def __init__(self, local_blocks=None, search_index=True, **kwargs):
        self._constructor_kwargs = kwargs
        self.search_index = search_index

        super().__init__(**kwargs)

        # create a local (shallow) copy of base_blocks so that it can be supplemented by local_blocks
        self.child_blocks = self.base_blocks.copy()
        if local_blocks:
            for name, block in local_blocks:
                block.set_name(name)
                self.child_blocks[name] = block

    @classmethod
    def construct_from_lookup(cls, lookup, child_blocks, **kwargs):
        if child_blocks:
            child_blocks = [
                (name, lookup.get_block(index)) for name, index in child_blocks
            ]
        return cls(child_blocks, **kwargs)

    def get_default(self):
        """
        Any default value passed in the constructor or self.meta is going to be a dict
        rather than a StructValue; for consistency, we need to convert it to a StructValue
        for StructBlock to work with
        """
        default = self._evaluate_callable(self.meta.default)

        return self.normalize(
            {
                name: default[name] if name in default else block.get_default()
                for name, block in self.child_blocks.items()
            }
        )

    def value_from_datadict(self, data, files, prefix):
        return self._to_struct_value(
            [
                (
                    name,
                    block.value_from_datadict(data, files, f"{prefix}-{name}"),
                )
                for name, block in self.child_blocks.items()
            ]
        )

    def value_omitted_from_data(self, data, files, prefix):
        return all(
            block.value_omitted_from_data(data, files, f"{prefix}-{name}")
            for name, block in self.child_blocks.items()
        )

    def clean(self, value):
        result = []  # build up a list of (name, value) tuples to be passed to the StructValue constructor
        errors = {}
        for name, val in value.items():
            try:
                result.append((name, self.child_blocks[name].clean(val)))
            except ValidationError as e:
                errors[name] = e

        if errors:
            raise StructBlockValidationError(errors)

        return self._to_struct_value(result)

    def to_python(self, value):
        """Recursively call to_python on children and return as a StructValue"""
        return self._to_struct_value(
            [
                (
                    name,
                    (
                        child_block.to_python(value[name])
                        if name in value
                        else child_block.get_default()
                    ),
                    # NB the result of get_default is NOT passed through to_python, as it's expected
                    # to be in the block's native type already
                )
                for name, child_block in self.child_blocks.items()
            ]
        )

    def bulk_to_python(self, values):
        # values is a list of dicts; split this into a series of per-subfield lists so that we can
        # call bulk_to_python on each subfield

        values_by_subfield = {}
        for name, child_block in self.child_blocks.items():
            # We need to keep track of which dicts actually have an item for this field, as missing
            # values will be populated with child_block.get_default(); this is expected to be a
            # value in the block's native type, and should therefore not undergo conversion via
            # bulk_to_python.
            indexes = []
            raw_values = []
            for i, val in enumerate(values):
                if name in val:
                    indexes.append(i)
                    raw_values.append(val[name])

            converted_values = child_block.bulk_to_python(raw_values)
            # create a mapping from original index to converted value
            converted_values_by_index = dict(zip(indexes, converted_values))

            # now loop over all list indexes, falling back on the default for any indexes not in
            # the mapping, to arrive at the final list for this subfield
            values_by_subfield[name] = []
            for i in range(0, len(values)):
                try:
                    converted_value = converted_values_by_index[i]
                except KeyError:
                    converted_value = child_block.get_default()

                values_by_subfield[name].append(converted_value)

        # now form the final list of StructValues, with each one constructed by taking the
        # appropriately-indexed item from all of the per-subfield lists
        return [
            self._to_struct_value(
                {name: values_by_subfield[name][i] for name in self.child_blocks.keys()}
            )
            for i in range(0, len(values))
        ]

    def _to_struct_value(self, block_items):
        """Return a Structvalue representation of the sub-blocks in this block"""
        return self.meta.value_class(self, block_items)

    def get_prep_value(self, value):
        """Recursively call get_prep_value on children and return as a plain dict"""
        return {
            name: self.child_blocks[name].get_prep_value(val)
            for name, val in value.items()
        }

    def normalize(self, value):
        if isinstance(value, self.meta.value_class):
            return value

        return self._to_struct_value(
            {k: self.child_blocks[k].normalize(v) for k, v in value.items()}
        )

    def get_form_state(self, value):
        return {
            name: self.child_blocks[name].get_form_state(val)
            for name, val in value.items()
        }

    def get_api_representation(self, value, context=None):
        """Recursively call get_api_representation on children and return as a plain dict"""
        return {
            name: self.child_blocks[name].get_api_representation(val, context=context)
            for name, val in value.items()
        }

    def get_searchable_content(self, value):
        if not self.search_index:
            return []
        content = []

        for name, block in self.child_blocks.items():
            content.extend(
                block.get_searchable_content(value.get(name, block.get_default()))
            )

        return content

    def extract_references(self, value):
        for name, block in self.child_blocks.items():
            for model, object_id, model_path, content_path in block.extract_references(
                value.get(name, block.get_default())
            ):
                model_path = f"{name}.{model_path}" if model_path else name
                content_path = f"{name}.{content_path}" if content_path else name
                yield model, object_id, model_path, content_path

    def get_block_by_content_path(self, value, path_elements):
        """
        Given a list of elements from a content path, retrieve the block at that path
        as a BoundBlock object, or None if the path does not correspond to a valid block.
        """
        if path_elements:
            name, *remaining_elements = path_elements
            try:
                child_block = self.child_blocks[name]
            except KeyError:
                return None

            child_value = value.get(name, child_block.get_default())
            return child_block.get_block_by_content_path(
                child_value, remaining_elements
            )
        else:
            # an empty path refers to the struct as a whole
            return self.bind(value)

    def deconstruct(self):
        """
        Always deconstruct StructBlock instances as if they were plain StructBlocks with all of the
        field definitions passed to the constructor - even if in reality this is a subclass of StructBlock
        with the fields defined declaratively, or some combination of the two.

        This ensures that the field definitions get frozen into migrations, rather than leaving a reference
        to a custom subclass in the user's models.py that may or may not stick around.
        """
        path = "wagtail.blocks.StructBlock"
        args = [list(self.child_blocks.items())]
        kwargs = self._constructor_kwargs
        return (path, args, kwargs)

    def deconstruct_with_lookup(self, lookup):
        path = "wagtail.blocks.StructBlock"
        args = [
            [
                (name, lookup.add_block(block))
                for name, block in self.child_blocks.items()
            ]
        ]
        kwargs = self._constructor_kwargs
        return (path, args, kwargs)

    def check(self, **kwargs):
        errors = super().check(**kwargs)
        for name, child_block in self.child_blocks.items():
            errors.extend(child_block.check(**kwargs))
            errors.extend(child_block._check_name(**kwargs))

        return errors

    def render_basic(self, value, context=None):
        return format_html(
            "<dl>\n{}\n</dl>",
            format_html_join("\n", "    <dt>{}</dt>\n    <dd>{}</dd>", value.items()),
        )

    def render_form_template(self):
        # Support for custom form_template options in meta. Originally form_template would have been
        # invoked once for each occurrence of this block in the stream data, but this rendering now
        # happens client-side, so we need to turn the Django template into one that can be used by
        # the client-side code. This is done by rendering it up-front with placeholder objects as
        # child blocks - these return <div data-structblock-child="first-name"></div> from their
        # render_form_method.
        # The change to client-side rendering means that the `value` and `errors` arguments on
        # `get_form_context` no longer receive real data; these are passed the block's default value
        # and None respectively.
        context = self.get_form_context(
            self.get_default(), prefix="__PREFIX__", errors=None
        )
        return mark_safe(render_to_string(self.meta.form_template, context))

    def get_description(self):
        return super().get_description() or getattr(self.meta, "help_text", "")

    def get_form_context(self, value, prefix="", errors=None):
        return {
            "children": collections.OrderedDict(
                [
                    (
                        name,
                        PlaceholderBoundBlock(
                            block, value.get(name), prefix=f"{prefix}-{name}"
                        ),
                    )
                    for name, block in self.child_blocks.items()
                ]
            ),
            "help_text": getattr(self.meta, "help_text", None),
            "classname": self.meta.form_classname,
            "collapsed": self.meta.collapsed,
            "block_definition": self,
            "prefix": prefix,
        }

    @cached_property
    def _has_default(self):
        return self.meta.default is not BaseStructBlock._meta_class.default

    class Meta:
        default = {}
        form_classname = "struct-block"
        form_template = None
        value_class = StructValue
        label_format = None
        collapsed = False
        # No icon specified here, because that depends on the purpose that the
        # block is being used for. Feel encouraged to specify an icon in your
        # descendant block type
        icon = "placeholder"


class StructBlock(BaseStructBlock, metaclass=DeclarativeSubBlocksMetaclass):
    pass


class StructBlockAdapter(Adapter):
    js_constructor = "wagtail.blocks.StructBlock"

    def js_args(self, block):
        meta = {
            "label": block.label,
            "description": block.get_description(),
            "required": block.required,
            "icon": block.meta.icon,
            "blockDefId": block.definition_prefix,
            "isPreviewable": block.is_previewable,
            "classname": block.meta.form_classname,
            "collapsed": block.meta.collapsed,
            "attrs": block.meta.form_attrs or {},
        }

        help_text = getattr(block.meta, "help_text", None)
        if help_text:
            meta["helpText"] = help_text
            meta["helpIcon"] = get_help_icon()

        if block.meta.form_template:
            meta["formTemplate"] = block.render_form_template()

        if block.meta.label_format:
            meta["labelFormat"] = block.meta.label_format

        return [
            block.name,
            block.child_blocks.values(),
            meta,
        ]

    @cached_property
    def media(self):
        return forms.Media(
            js=[
                versioned_static("wagtailadmin/js/telepath/blocks.js"),
            ]
        )


register(StructBlockAdapter(), StructBlock)
