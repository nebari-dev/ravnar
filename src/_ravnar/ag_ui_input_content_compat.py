__all__ = [
    "AudioInputContent",
    "AudioInputPart",
    "DocumentInputContent",
    "DocumentInputPart",
    "ImageInputContent",
    "ImageInputPart",
    "InputContent",
    "InputContentCustomSource",
    "InputContentPart",
    "InputContentSource",
    "VideoInputContent",
    "VideoInputPart",
]

from typing import Annotated, Any, Literal

import ag_ui.core
from pydantic import Field


class InputContentCustomSource(ag_ui.core.types.ConfiguredBaseModel):
    """Custom source."""

    type: Literal["custom"] = "custom"
    name: str
    value: Any


InputContentSource = Annotated[
    ag_ui.core.InputContentDataSource | ag_ui.core.InputContentUrlSource | InputContentCustomSource,
    Field(discriminator="type"),
]


class ImageInputContent(ag_ui.core.ImageInputContent):
    """An image input content fragment."""

    source: InputContentSource  # type: ignore[assignment]


class AudioInputContent(ag_ui.core.AudioInputContent):
    """An audio input content fragment."""

    source: InputContentSource  # type: ignore[assignment]


class VideoInputContent(ag_ui.core.VideoInputContent):
    """A video input content fragment."""

    source: InputContentSource  # type: ignore[assignment]


class DocumentInputContent(ag_ui.core.DocumentInputContent):
    """A document input content fragment."""

    source: InputContentSource  # type: ignore[assignment]


InputContent = Annotated[
    ag_ui.core.TextInputContent
    | ImageInputContent
    | AudioInputContent
    | VideoInputContent
    | DocumentInputContent
    | ag_ui.core.BinaryInputContent,
    Field(discriminator="type"),
]

ImageInputPart = ImageInputContent
AudioInputPart = AudioInputContent
VideoInputPart = VideoInputContent
DocumentInputPart = DocumentInputContent

InputContentPart = InputContent
