"""Poll scheduler: priority weighting, passive-first suppression, one at a time."""
from aioebus.messages import parse_csv
from aioebus.poll import PollScheduler

CSV = """\
type,circuit,level,name,comment,qq,zz,pbsb,id,*name,part,type,divisor/values,unit,comment
r1,elo,,Fast,,,38,b509,0d1800,value,,UCH,,,
r5,elo,,Slow,,,38,b509,0d1900,value,,UCH,,,
r,elo,,OnDemand,,,38,b509,0d2000,value,,UCH,,,
w,elo,,Write,,,38,b509,0e2000,value,,UCH,,,
"""
MSGS = parse_csv(CSV)


def test_only_polled_priorities_scheduled():
    sch = PollScheduler(MSGS, base_interval=10.0)
    # plain r (OnDemand) and w (Write) are not auto-polled by default
    assert set(k[1] for k in sch._items) == {"Fast", "Slow"}


def test_default_priority_polls_plain_reads():
    sch = PollScheduler(MSGS, base_interval=10.0, default_priority=7)
    # plain r (OnDemand) is now scheduled at the default priority; w stays out
    assert set(k[1] for k in sch._items) == {"Fast", "Slow", "OnDemand"}
    item = sch._items[("38", "OnDemand")]
    assert item.priority == 7 and sch.interval(item) == 70.0
    # an r1 keeps its own digit, not the default
    assert sch._items[("38", "Fast")].priority == 1


def test_priority_sets_interval():
    sch = PollScheduler(MSGS, base_interval=10.0)
    # at t=12: Fast (interval 10) is due, Slow (interval 50) is not
    due = sch.next_due(12.0)
    assert due is not None and due.msg.name == "Fast"
    # at t=60: both due -> most overdue wins (Slow: 60/50=1.2 > Fast 60/10=6? no)
    # Fast score 6.0 >> Slow 1.2, so Fast is picked
    assert sch.next_due(60.0).msg.name == "Fast"


def test_failed_poll_backoff():
    sch = PollScheduler(MSGS, base_interval=10.0)
    fast = next(m for m in MSGS if m.name == "Fast")  # r1 -> base interval 10
    item = sch._items[("38", "Fast")]
    assert sch.interval(item) == 10.0
    sch.mark_failure(fast, now=0.0)
    assert sch.interval(item) == 20.0          # 2x after one unanswered poll
    sch.mark_failure(fast, now=0.0)
    assert sch.interval(item) == 40.0          # 4x
    sch.mark_passive(fast, now=0.0)            # a value clears the backoff
    assert sch.interval(item) == 10.0


def test_passive_update_suppresses_poll():
    sch = PollScheduler(MSGS, base_interval=10.0)
    assert sch.next_due(12.0).msg.name == "Fast"   # Fast would be polled at t=12
    fast = next(m for m in MSGS if m.name == "Fast")
    sch.mark_passive(fast, now=12.0)               # but it was seen on the bus
    assert sch.next_due(13.0) is None              # so no poll is due anymore


def test_rebuild_carries_timing_over():
    old = PollScheduler(MSGS, base_interval=10.0)
    fast = next(m for m in MSGS if m.name == "Fast")
    old.mark_passive(fast, now=100.0)
    old.mark_failure(next(m for m in MSGS if m.name == "Slow"), now=100.0)
    new = PollScheduler(MSGS, base_interval=10.0, default_priority=7)
    new.carry_from(old)
    assert new._items[("38", "Fast")].last_update == 100.0   # not due again at once
    assert new._items[("38", "Slow")].fails == 1              # backoff kept
    assert new._items[("38", "OnDemand")].last_update == 0.0  # new register: due now


def test_one_at_a_time_and_due_count():
    sch = PollScheduler(MSGS, base_interval=10.0)
    assert sch.due_count(60.0) == 2       # both overdue
    picked = sch.next_due(60.0)           # but only one handed back
    assert picked is not None
    sch.mark(picked.msg.zz, picked.msg.name, 60.0)
    assert sch.due_count(60.0) == 1
