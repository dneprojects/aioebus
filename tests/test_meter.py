"""Bus meter: load, our poll rates per device, other masters' unanswered reads."""
from aioebus.frame import SYN
from aioebus.meter import BusMeter


def test_load_is_the_data_against_the_bus_capacity():
    # 2400 baud carry 240 symbols a second; an adapter may pass few idle SYN on
    m = BusMeter()
    assert m.utilization(0.0) is None
    for i in range(60):  # 60 data symbols a second for a minute, one SYN
        for _ in range(60):
            m.symbol(0x10, float(i))
    m.symbol(SYN, 59.9)
    assert m.utilization(60.0) == 25.0
    m.symbol(SYN, 200.0)  # then idle: the old data falls out
    assert m.utilization(200.0) == 0.0


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
    m.foreign_unanswered(0x15, 5.0)
    m.foreign_unanswered(0x0B, 9.0)  # a controller probing an empty address
    assert m.unanswered_per_minute(10.0) == 12.0  # 2 in 10 s
    assert m.unanswered_per_minute(10.0, {0x15, 0x08}) == 6.0  # devices that are there
    assert BusMeter().unanswered_per_minute(0.0) is None  # nothing seen yet
