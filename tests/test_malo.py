import pytest

from billing_agent import malo


def test_bdew_worked_example():
    # Example from the BDEW Anwendungshilfe MaLo-ID (2017).
    assert malo.check_digit("4137355924") == 1
    assert malo.is_valid("41373559241")


@pytest.mark.parametrize(
    "value",
    ["41373559242", "01373559241", "4137355924", "413735592411", "4137355924X", "", None],
)
def test_invalid(value):
    assert not malo.is_valid(value)


def test_distance_of_ten_becomes_zero():
    assert malo.check_digit("5500000000") == 5  # 5 + 2*5 = 15 -> 5
    assert malo.check_digit("1300000000") == 3  # 1 + 6 = 7 -> 3
    assert malo.check_digit("2400000000") == 0  # 2 + 8 = 10 -> 0


def test_with_check_digit_round_trips():
    assert malo.is_valid(malo.with_check_digit("9876543210"))
