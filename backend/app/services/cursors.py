"""Opaque keyset cursors.

List endpoints are paginated by keyset, not by `OFFSET`, for two reasons: skipping
thousands of rows costs the same as reading the first page (the index is walked from the
cursor position), and a row inserted while somebody is paging cannot cause an item to be
shown twice or skipped entirely — which `OFFSET` does, because it counts rows rather than
identifying a position.

The position is the pair `(created_at, id)`. `id` is what makes it a *total* order: two
items created in the same microsecond still have exactly one correct sequence.

Most tables are keyed by a UUID, but `activity_events` uses a bigint identity, so the id
half of a cursor is parsed as whichever the list says it is. A cursor with the wrong kind is
a validation error at the boundary instead of reaching SQL as a `bigint < uuid` comparison.
UUID-keyed lists share a syntax and are structurally interchangeable, so clients must keep
each cursor with the list that issued it; that is what "activity paginates independently of
item lists" means in practice.

The cursor is base64url so that clients treat it as opaque. It is not signed: it contains
no secret and no permission decision, only a position within a result set the caller is
already authorized to read. Tampering can only produce a different valid position (or a
validation error), never access to another team's rows, because the team filter is applied
separately from the cursor.
"""

from __future__ import annotations

import base64
import binascii
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from app.errors import ValidationFailed

_SEPARATOR = "|"

#: Which flavour of id a cursor carries. Every paginated table is keyed by a UUID except
#: `activity_events`, which uses a bigint identity. A cursor has to say which it is, so a
#: cursor with the wrong kind is refused at the boundary instead of reaching SQL as a
#: `bigint < uuid` comparison.
IdKind = Literal["uuid", "int"]


@dataclass(frozen=True, slots=True)
class Cursor:
    created_at: datetime
    #: A `uuid.UUID` for UUID-keyed tables, an `int` for `activity_events`.
    id: uuid.UUID | int


def encode_cursor(created_at: datetime, id: uuid.UUID | int) -> str:
    raw = f"{created_at.isoformat()}{_SEPARATOR}{id}".encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _parse_id(text: str, id_kind: IdKind) -> uuid.UUID | int:
    if id_kind == "uuid":
        try:
            return uuid.UUID(text)
        except ValueError as exc:
            raise ValidationFailed("The pagination cursor is not valid.") from exc
    if text.isascii() and text.isdigit():
        value = int(text)
        # PostgreSQL bigint is signed 64-bit; reject values outside that range
        if value < -9223372036854775808 or value > 9223372036854775807:
            raise ValidationFailed("The pagination cursor is not valid.")
        return value
    raise ValidationFailed("The pagination cursor is not valid.")


def decode_cursor(value: str, *, id_kind: IdKind) -> Cursor:
    """Parse a cursor for a list whose rows are keyed by `id_kind`, or raise.

    A malformed cursor is the caller's mistake and is reported as such rather than
    being silently ignored, which would quietly restart the list from the beginning. The
    same is true of a *well-formed* cursor whose id kind does not match. UUID-keyed lists
    share a syntax, so a cursor from one is structurally valid on another but names a
    different position; clients must keep each cursor with its list.
    """
    padded = value + "=" * (-len(value) % 4)
    try:
        decoded = base64.urlsafe_b64decode(padded).decode()
    except (binascii.Error, UnicodeDecodeError, ValueError) as exc:
        raise ValidationFailed("The pagination cursor is not valid.") from exc

    created_at_text, separator, id_text = decoded.partition(_SEPARATOR)
    if not separator:
        raise ValidationFailed("The pagination cursor is not valid.")

    try:
        created_at = datetime.fromisoformat(created_at_text)
    except ValueError as exc:
        raise ValidationFailed("The pagination cursor is not valid.") from exc

    return Cursor(created_at=created_at, id=_parse_id(id_text, id_kind))


__all__ = ["Cursor", "IdKind", "decode_cursor", "encode_cursor"]
