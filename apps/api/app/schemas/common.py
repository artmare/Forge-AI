from typing import Annotated, ClassVar, Self

from pydantic import BaseModel, ConfigDict, StringConstraints, model_validator

NonEmptyText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
Slug = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        min_length=1,
        max_length=120,
        pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$",
    ),
]


class ORMResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class PartialUpdate(ORMResponse):
    non_nullable_fields: ClassVar[frozenset[str]] = frozenset()

    @model_validator(mode="after")
    def reject_null_for_required_fields(self) -> Self:
        null_fields = sorted(
            field
            for field in self.non_nullable_fields & self.model_fields_set
            if getattr(self, field) is None
        )
        if null_fields:
            fields = ", ".join(null_fields)
            raise ValueError(f"Fields cannot be null: {fields}.")
        return self
