from billing_agent.cli import main


def test_help_runs():
    assert main([]) == 0
