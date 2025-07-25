import os
from io import BytesIO

import willow
from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import FileExtensionValidator
from django.forms.fields import FileField, ImageField
from django.forms.widgets import FileInput
from django.template.defaultfilters import filesizeformat
from django.utils.translation import gettext_lazy as _

from wagtail.images.utils import get_accept_attributes, get_allowed_image_extensions


def ImageFileExtensionValidator(value):
    # This makes testing different values of WAGTAILIMAGES_EXTENSIONS easier:
    # if WagtailImageField.default_validators
    #      = FileExtensionValidator(get_allowed_image_extensions())
    # then the formats that will pass validation are fixed at the time the class
    # is created, so changes to WAGTAILIMAGES_EXTENSIONS via override_settings
    # has no effect.
    return FileExtensionValidator(get_allowed_image_extensions())(value)


class WagtailImageField(ImageField):
    default_validators = [ImageFileExtensionValidator]

    def __init__(self, *args, **kwargs):
        self.allowed_image_extensions = get_allowed_image_extensions()

        super().__init__(*args, **kwargs)

        # Get max upload size from settings
        self.max_upload_size = getattr(
            settings, "WAGTAILIMAGES_MAX_UPLOAD_SIZE", 10 * 1024 * 1024
        )
        self.max_image_pixels = getattr(
            settings, "WAGTAILIMAGES_MAX_IMAGE_PIXELS", 128 * 1000000
        )
        self.max_upload_size_text = filesizeformat(self.max_upload_size)

        self.supported_formats_text = ", ".join(self.allowed_image_extensions).upper()

        # Help text
        if self.max_upload_size is not None:
            self.help_text = _(
                "Supported formats: %(supported_formats)s. Maximum filesize: %(max_upload_size)s."
            ) % {
                "supported_formats": self.supported_formats_text,
                "max_upload_size": self.max_upload_size_text,
            }
        else:
            self.help_text = _("Supported formats: %(supported_formats)s.") % {
                "supported_formats": self.supported_formats_text,
            }

        # Error messages
        # Translation placeholders should all be interpolated at the same time to avoid escaping,
        # either right now if all values are known, otherwise when used.
        self.error_messages["invalid_image_extension"] = _(
            "Not a supported image format. Supported formats: %(supported_formats)s."
        ) % {"supported_formats": self.supported_formats_text}

        self.error_messages["invalid_image_known_format"] = _(
            "Not a valid .%(extension)s image. The extension does not match the file format (%(image_format)s)"
        )

        self.error_messages["file_too_large"] = _(
            "This file is too big (%(file_size)s). Maximum filesize %(max_filesize)s."
        )

        self.error_messages["file_too_many_pixels"] = _(
            "This file has too many pixels (%(num_pixels)s). Maximum pixels %(max_pixels_count)s."
        )

        self.error_messages["file_too_large_unknown_size"] = _(
            "This file is too big. Maximum filesize %(max_filesize)s."
        ) % {"max_filesize": self.max_upload_size_text}

    def check_image_file_format(self, f):
        # Check file extension
        extension = os.path.splitext(f.name)[1].lower()[1:]

        if extension not in self.allowed_image_extensions:
            raise ValidationError(
                self.error_messages["invalid_image_extension"],
                code="invalid_image_extension",
            )

        if extension == "jpg":
            extension = "jpeg"

        # Check that the internal format matches the extension
        # It is possible to upload PSD files if their extension is set to jpg, png or gif. This should catch them out
        if extension != f.image.format_name:
            raise ValidationError(
                self.error_messages["invalid_image_known_format"]
                % {"extension": extension, "image_format": f.image.format_name},
                code="invalid_image_known_format",
            )

    def check_image_file_size(self, f):
        # Upload size checking can be disabled by setting max upload size to None
        if self.max_upload_size is None:
            return

        # Check the filesize
        if f.size > self.max_upload_size:
            raise ValidationError(
                self.error_messages["file_too_large"]
                % {
                    "file_size": filesizeformat(f.size),
                    "max_filesize": self.max_upload_size_text,
                },
                code="file_too_large",
            )

    def check_image_pixel_size(self, f):
        # Upload pixel size checking can be disabled by setting max upload pixel to None
        if self.max_image_pixels is None:
            return

        # Check the pixel size
        width, height = f.image.get_size()
        frames = f.image.get_frame_count()
        num_pixels = width * height * frames

        if num_pixels > self.max_image_pixels:
            raise ValidationError(
                self.error_messages["file_too_many_pixels"]
                % {"num_pixels": num_pixels, "max_pixels_count": self.max_image_pixels},
                code="file_too_many_pixels",
            )

    def to_python(self, data):
        """
        Check that the file-upload field data contains a valid image (GIF, JPG,
        PNG, etc. -- whatever Willow supports). Overridden from ImageField to use
        Willow instead of Pillow as the image library in order to enable SVG support.
        """
        f = FileField.to_python(self, data)
        if f is None:
            return None

        # Get the file content ready for Willow
        if hasattr(data, "temporary_file_path"):
            # Django's `TemporaryUploadedFile` is enough of a file to satisfy Willow
            # Willow doesn't support opening images by path https://github.com/wagtail/Willow/issues/108
            file = data
        else:
            if hasattr(data, "read"):
                file = BytesIO(data.read())
            else:
                file = BytesIO(data["content"])

        try:
            # Annotate the python representation of the FileField with the image
            # property so subclasses can reuse it for their own validation
            f.image = willow.Image.open(file)
            f.content_type = f.image.mime_type

        except Exception as exc:  # noqa: BLE001
            # Willow doesn't recognize it as an image.
            raise ValidationError(
                self.error_messages["invalid_image"],
                code="invalid_image",
            ) from exc

        if hasattr(f, "seek") and callable(f.seek):
            f.seek(0)

        if f is not None:
            self.check_image_file_size(f)
            self.check_image_file_format(f)
            self.check_image_pixel_size(f)

        return f

    def widget_attrs(self, widget):
        attrs = super().widget_attrs(widget)

        if (
            isinstance(widget, FileInput)
            and "accept" not in widget.attrs
            and attrs.get("accept") == "image/*"
        ):
            # File upload dialogs will often not allow selecting heic or avif if the accept attribute is
            # given as "image/*" - we need to add explicit mimetypes for these
            attrs["accept"] = get_accept_attributes()

        return attrs
