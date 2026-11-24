import json
import struct
from datetime import UTC, datetime
from io import BytesIO
from typing import Annotated
from uuid import UUID

from fastavro import parse_schema, schemaless_writer
from fastavro.validation import validate
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, StringConstraints, field_validator

from .settings import MAX_MOVIE_ID_BYTES, ROOT

SCHEMA_TEXT = (ROOT / "schemas/movie-event-v1.avsc").read_text(encoding="utf-8")
AVRO_SCHEMA = parse_schema(json.loads(SCHEMA_TEXT))
FIELDS = {field["name"]: field["type"] for field in AVRO_SCHEMA["fields"]}
DEVICES = tuple(FIELDS["device_type"]["symbols"])
Identifier = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class Event(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event_id: UUID
    user_id: Identifier
    movie_id: Identifier
    event_type: str
    timestamp: AwareDatetime
    device_type: str
    session_id: Identifier
    progress_seconds: int = Field(strict=True, ge=0, le=2**31 - 1)

    @field_validator("movie_id")
    @classmethod
    def check_movie_id(cls, value):
        if "\x00" in value or len(value.encode("utf-8")) > MAX_MOVIE_ID_BYTES:
            raise ValueError(f"Movie ID must contain no NUL and use at most {MAX_MOVIE_ID_BYTES} UTF-8 bytes")
        return value

    @field_validator("event_type", "device_type")
    @classmethod
    def check_enum(cls, value, info):
        symbols = FIELDS[info.field_name]["symbols"]
        if value not in symbols:
            raise ValueError(f"Expected one of: {', '.join(symbols)}")
        return value

    @field_validator("timestamp", mode="before")
    @classmethod
    def check_timestamp_input(cls, value):
        if not isinstance(value, datetime) and not (isinstance(value, str) and len(value) >= 19):
            raise ValueError("Use an ISO 8601 datetime with a UTC offset")
        return value

    @field_validator("timestamp")
    @classmethod
    def normalize_timestamp(cls, value):
        if not 1900 <= value.year <= 2299:
            raise ValueError("Timestamp must be between 1900 and 2299 UTC")
        value = value.astimezone(UTC)
        if not 1900 <= value.year <= 2299:
            raise ValueError("Timestamp must be between 1900 and 2299 UTC")
        return value


def encode(event: Event, schema_id: int) -> bytes:
    record = event.model_dump()
    validate(record, AVRO_SCHEMA, strict=True)
    data = BytesIO()
    data.write(struct.pack(">BI", 0, schema_id))
    schemaless_writer(data, AVRO_SCHEMA, record, strict=True)
    return data.getvalue()
