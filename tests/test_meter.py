"""Bus meter: load, our poll rates per device, other masters' unanswered reads."""
from aioebus.frame import SYN
from aioebus.meter import BusMeter


def test_load_is_the_share_of_data_symbols_over_the_last_minute():
    m = BusMeter()
    assert m.utilization(0.0) is None
    for i in range(30):  # one data symbol and three SYN a second for 30 s
        m.symbol(0x10, float(i))
        for _ in range(3):
            m.symbol(SYN, i + 0.5)
    assert m.utilization(30.0) == 25.0
    for i in range(30, 100):  # then an idle bus: the old data falls out
        m.symbol(SYN, float(i))
    assert m.utilization(100.0) == 0.0


def test_polls_per_minute_overall_and_per_device():
    m = BusMeter()
    m.symbol(SYN, 0.0)
    for t in range(0, 60, 12):  # the controller every 12 s
        m.poll(0x15, float(t))
    for t in range(60):  # the heat pump every second
        m.poll(0x08, float(t))
    assert m.polls_per_minute(60.0, 0x15) == 5.0
    assert m.polls_per_minute(60.0) == 65.0
    assert m.polls_per_minute(200.0) == 0.0  # all older than a minute


def test_rates_right_after_the_start_are_scaled_to_a_minute():
    m = BusMeter()
    m.symbol(SYN, 0.0)
    m.foreign_unanswered(5.0)
    m.foreign_unanswered(9.0)
    assert m.unanswered_per_minute(10.0) == 12.0  # 2 in 10 s
    assert BusMeter().unanswered_per_minute(0.0) is None  # nothing seen yet
