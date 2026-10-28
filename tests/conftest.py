import pytest

from billing_agent.db import connect
from billing_agent.generate import generate

MONTH = "2026-09"


@pytest.fixture
def con():
    con = connect(":memory:")
    yield con
    con.close()


@pytest.fixture
def demo_con(con):
    generate(con, MONTH)
    return con
