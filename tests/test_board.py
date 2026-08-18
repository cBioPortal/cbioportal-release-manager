import datetime

from release import board

from .conftest import TODAY


def test_current_iteration_is_the_one_today_falls_in():
    iterations = [
        {"id": "now", "title": "now",
         "startDate": str(TODAY - datetime.timedelta(days=2)), "duration": 7},
        {"id": "next", "title": "next",
         "startDate": str(TODAY + datetime.timedelta(days=5)), "duration": 7},
    ]
    assert board._current_iteration(iterations)["id"] == "now"


def test_between_sprints_falls_forward_to_the_next_one():
    iterations = [
        {"id": "next", "title": "next",
         "startDate": str(TODAY + datetime.timedelta(days=3)), "duration": 7},
    ]
    assert board._current_iteration(iterations)["id"] == "next"
    assert board._current_iteration([]) is None
