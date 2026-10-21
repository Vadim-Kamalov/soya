import re
from datetime import datetime
from decimal import Decimal

from pydantic import AwareDatetime, BaseModel, ConfigDict, field_serializer, field_validator

RFC3339 = re.compile(r"\d{4}-\d{2}-\d{2}[Tt]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:[Zz]|[+-]\d{2}:\d{2})")


class ContractModel(BaseModel):
    """Shared serialization policy for generated DTO, contains no API fields."""

    model_config = ConfigDict(extra="forbid", validate_default=True)

    @field_validator("*", mode="before")
    @classmethod
    def numeric_json_type(cls, value, info):
        if cls.model_fields[info.field_name].annotation is Decimal:
            if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
                raise ValueError("Must be a JSON number")
        if cls.model_fields[info.field_name].annotation in (datetime, AwareDatetime):
            if not isinstance(value, (str, datetime)):
                raise ValueError("Must be an RFC3339 date-time string with timezone")
            if isinstance(value, str) and not RFC3339.fullmatch(value):
                raise ValueError("Must be an RFC3339 date-time string with timezone")
        return value

    @field_serializer("*", mode="wrap", check_fields=False)
    def decimal_json(self, value, serialize):
        return float(value) if isinstance(value, Decimal) else serialize(value)
