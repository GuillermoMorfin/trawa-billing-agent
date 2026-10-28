import pytest
from conftest import MONTH

from billing_agent.prices import load_prices
from billing_agent.workflow import TransitionError, invoice_id, run_billing, status_of, transition


@pytest.fixture
def run_con(demo_con):
    load_prices(demo_con, MONTH)
    run_billing(demo_con, MONTH)
    return demo_con


def _status(con, cid):
    return status_of(con, invoice_id(MONTH, cid))


def test_first_statuses(run_con):
    for cid in ("C02", "C03", "C06", "C08", "C09", "C10"):
        assert _status(run_con, cid) == "blocked", cid
    for cid in ("C05", "C07"):
        assert _status(run_con, cid) == "needs_review", cid
    for cid in ("C01", "C04", "C11", "C12", "C13", "C14", "C15"):
        assert _status(run_con, cid) == "ready", cid


def test_blocked_invoice_can_never_be_approved(run_con):
    for actor in ("human:anna", "agent", "rules", "system"):
        with pytest.raises(TransitionError):
            transition(run_con, invoice_id(MONTH, "C03"), "approved", actor, "looks fine")


def test_only_people_approve(run_con):
    inv = invoice_id(MONTH, "C01")
    for actor in ("agent", "rules", "system"):
        with pytest.raises(TransitionError, match="may not"):
            transition(run_con, inv, "approved", actor)
    transition(run_con, inv, "approved", "human:anna")
    transition(run_con, inv, "issued", "human:anna")
    assert _status(run_con, "C01") == "issued"


def test_flagged_invoice_needs_a_note_to_approve(run_con):
    inv = invoice_id(MONTH, "C07")
    with pytest.raises(TransitionError, match="needs a note"):
        transition(run_con, inv, "approved", "human:anna")
    transition(run_con, inv, "approved", "human:anna", "New production line since September")
    assert _status(run_con, "C07") == "approved"


def test_rerun_keeps_approved_and_rebuilds_the_rest(run_con):
    transition(run_con, invoice_id(MONTH, "C01"), "approved", "human:anna")
    gross_before = run_con.execute(
        "SELECT gross_eur FROM invoices WHERE customer_id = 'C01'"
    ).fetchone()[0]

    # Fix C10's tariff and change C01's price, then re-run.
    run_con.execute(
        "UPDATE contracts SET contract_type = 'spot', margin_ct_kwh = 1.3 WHERE customer_id = 'C10'"
    )
    run_con.execute("UPDATE contracts SET fixed_ct_kwh = 99 WHERE customer_id = 'C01'")
    counts = run_billing(run_con, MONTH)

    assert counts["kept_final"] == 1
    assert _status(run_con, "C01") == "approved"
    assert (
        run_con.execute("SELECT gross_eur FROM invoices WHERE customer_id = 'C01'").fetchone()[0]
        == gross_before
    )
    assert _status(run_con, "C10") == "ready"
    (n,) = run_con.execute("SELECT count(*) FROM invoices").fetchone()
    assert n == 15


def test_every_status_change_is_logged(run_con):
    inv = invoice_id(MONTH, "C01")
    transition(run_con, inv, "approved", "human:anna")
    rows = run_con.execute(
        """SELECT actor, action, from_status, to_status FROM audit_log
           WHERE invoice_id = ? ORDER BY entry_id""",
        [inv],
    ).fetchall()
    assert rows == [
        ("system", "calculated", None, "draft"),
        ("system", "status", "draft", "ready"),
        ("human:anna", "status", "ready", "approved"),
    ]
