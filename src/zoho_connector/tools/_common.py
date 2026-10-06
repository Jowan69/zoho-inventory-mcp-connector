"""Input validation and pagination helpers shared by the tools."""

from collections.abc import Mapping
from datetime import date
from typing import Annotated, Any, TypeVar

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    ValidationError,
)

from zoho_connector.client.zoho_client import ZohoClient
from zoho_connector.errors import InvalidInputError

MAX_PER_PAGE = 100
DEFAULT_PER_PAGE = 25
MIN_QUERY_LENGTH = 2

M = TypeVar("M", bound=BaseModel)


def _iso_date(value: str) -> str:
    stripped = value.strip()
    if len(stripped) != 10:
        raise ValueError("must be an ISO date, YYYY-MM-DD")
    try:
        return date.fromisoformat(stripped).isoformat()
    except ValueError as exc:
        raise ValueError("must be a real ISO date, YYYY-MM-DD") from exc


IsoDate = Annotated[str, AfterValidator(_iso_date)]
Query = Annotated[str, StringConstraints(strip_whitespace=True, min_length=MIN_QUERY_LENGTH)]
# Zoho ids are numeric; checking this also keeps odd characters out of the request path.
ZohoId = Annotated[str, StringConstraints(strip_whitespace=True, pattern=r"^\d{1,32}$")]


class Paging(BaseModel):
    model_config = ConfigDict(extra="forbid")

    page: int = Field(default=1, ge=1)
    per_page: int = Field(default=DEFAULT_PER_PAGE, ge=1, le=MAX_PER_PAGE)


def validated(model: type[M], **data: Any) -> M:
    """Build a pydantic input model, turning validation failures into INVALID_INPUT."""
    try:
        return model(**data)
    except ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(str(p) for p in err['loc']) or 'input'}: {err['msg']}"
            for err in exc.errors()
        )
        raise InvalidInputError(problems) from exc


def budget_warning(client: ZohoClient) -> str | None:
    quota = client.quota()
    if not quota["warning"]:
        return None
    used, budget = quota["used_today"], quota["budget"]
    percent = round(100 * used / budget) if budget else 100
    return (
        f"Zoho request budget is {percent}% used today ({used} of {budget}). "
        "Use narrower searches and avoid repeating calls."
    )


def page_extras(
    client: ZohoClient, body: Mapping[str, Any], page: int
) -> tuple[int | None, str | None]:
    """(next_page, warning) for a list response fetched at `page`."""
    context = body.get("page_context")
    has_more = isinstance(context, dict) and bool(context.get("has_more_page"))
    return (page + 1 if has_more else None), budget_warning(client)
