import pytest

from cliretry.cli import parser


def test_watch_accepts_comma_separated_tab_numbers():
    args = parser().parse_args(["watch", "--tabs", "1,3,5"])
    assert args.tabs == [1, 3, 5]
    assert args.session is None
    assert args.current is False


@pytest.mark.parametrize("value", ["", "0", "1,-2", "1,1", "one,2", "1,,2"])
def test_watch_rejects_invalid_tab_numbers(value):
    with pytest.raises(SystemExit):
        parser().parse_args(["watch", "--tabs", value])


def test_watch_tab_selector_is_mutually_exclusive_with_session_and_current():
    with pytest.raises(SystemExit):
        parser().parse_args(["watch", "--tabs", "1,2", "--session", "session-id"])
    with pytest.raises(SystemExit):
        parser().parse_args(["watch", "--tabs", "1,2", "--current"])
