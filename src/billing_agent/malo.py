"""Marktlokations-ID (MaLo-ID): the 11-digit ID of a German market location.

Check digit per the BDEW "Anwendungshilfe MaLo-ID" (2017): over the first 10 digits, add the digits
in odd positions, add twice the sum of the digits in even positions, and take the distance to the
next multiple of 10 (a distance of 10 becomes 0). Worked example from the guide: 4137355924 -> 1.
"""

from __future__ import annotations

import re

_FORMAT = re.compile(r"[1-9]\d{10}")


def check_digit(first_ten: str) -> int:
    odd = sum(int(c) for c in first_ten[0::2])
    even = sum(int(c) for c in first_ten[1::2])
    return (10 - (odd + 2 * even) % 10) % 10


def is_valid_format(malo_id: str | None) -> bool:
    return bool(malo_id) and _FORMAT.fullmatch(malo_id) is not None


def is_valid(malo_id: str | None) -> bool:
    return is_valid_format(malo_id) and check_digit(malo_id[:10]) == int(malo_id[10])


def with_check_digit(first_ten: str) -> str:
    return first_ten + str(check_digit(first_ten))
