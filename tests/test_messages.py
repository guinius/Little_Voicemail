import pytest

from src.messages import MessageQueue


@pytest.fixture
def queue(tmp_path):
    q = MessageQueue(tmp_path / "messages.db")
    yield q
    q.close()


def test_add_and_count(queue):
    queue.add(slot=3, sender="+447700900001", signal_ts=1000, attachment="/tmp/a.ogg")
    queue.add(slot=3, sender="+447700900001", signal_ts=2000, attachment="/tmp/b.ogg")
    queue.add(slot=7, sender="+447700900002", signal_ts=1500, attachment="/tmp/c.ogg")

    assert queue.pending_counts() == {3: 2, 7: 1}
    assert queue.total_pending() == 3


def test_redelivery_of_the_same_message_is_ignored(queue):
    first = queue.add(slot=1, sender="+441", signal_ts=555, attachment="/tmp/a.ogg")
    second = queue.add(slot=1, sender="+441", signal_ts=555, attachment="/tmp/a.ogg")

    assert first is not None
    assert second is None
    assert queue.total_pending() == 1


def test_pending_is_ordered_oldest_first(queue):
    queue.add(slot=2, sender="+441", signal_ts=3000, attachment="/tmp/third.ogg")
    queue.add(slot=2, sender="+441", signal_ts=1000, attachment="/tmp/first.ogg")
    queue.add(slot=2, sender="+441", signal_ts=2000, attachment="/tmp/second.ogg")

    order = [m.attachment for m in queue.pending_for_slot(2)]
    assert order == ["/tmp/first.ogg", "/tmp/second.ogg", "/tmp/third.ogg"]


def test_mark_played_removes_only_that_message(queue):
    first = queue.add(slot=4, sender="+441", signal_ts=1, attachment="/tmp/a.ogg")
    queue.add(slot=4, sender="+441", signal_ts=2, attachment="/tmp/b.ogg")

    queue.mark_played(first)

    remaining = queue.pending_for_slot(4)
    assert len(remaining) == 1
    assert remaining[0].attachment == "/tmp/b.ogg"


def test_read_elsewhere_clears_everything_up_to_that_timestamp(queue):
    """Reading one message on a phone implicitly reads the older ones too."""
    queue.add(slot=5, sender="+441", signal_ts=100, attachment="/tmp/a.ogg")
    queue.add(slot=5, sender="+441", signal_ts=200, attachment="/tmp/b.ogg")
    queue.add(slot=5, sender="+441", signal_ts=300, attachment="/tmp/c.ogg")

    cleared = queue.mark_read_elsewhere("+441", up_to_ts=200)

    assert cleared == 2
    assert [m.signal_ts for m in queue.pending_for_slot(5)] == [300]


def test_read_elsewhere_does_not_touch_other_senders(queue):
    queue.add(slot=1, sender="+441", signal_ts=100, attachment="/tmp/a.ogg")
    queue.add(slot=2, sender="+442", signal_ts=100, attachment="/tmp/b.ogg")

    queue.mark_read_elsewhere("+441", up_to_ts=999)

    assert queue.pending_counts() == {2: 1}


def test_clearing_is_idempotent(queue):
    queue.add(slot=1, sender="+441", signal_ts=100, attachment="/tmp/a.ogg")

    assert queue.clear_slot(1) == 1
    assert queue.clear_slot(1) == 0  # already cleared, not counted twice


def test_clear_all_empties_every_slot(queue):
    queue.add(slot=1, sender="+441", signal_ts=1, attachment="/tmp/a.ogg")
    queue.add(slot=9, sender="+442", signal_ts=2, attachment="/tmp/b.ogg")

    assert queue.clear_all() == 2
    assert queue.pending_counts() == {}


def test_queue_survives_reopening(tmp_path):
    """A message that arrives at bedtime must still be there in the morning."""
    path = tmp_path / "messages.db"
    first = MessageQueue(path)
    first.add(slot=6, sender="+441", signal_ts=42, attachment="/tmp/a.ogg")
    first.close()

    second = MessageQueue(path)
    try:
        assert second.pending_counts() == {6: 1}
    finally:
        second.close()


def test_prune_only_drops_old_cleared_rows(queue):
    kept = queue.add(slot=1, sender="+441", signal_ts=1, attachment="/tmp/a.ogg")
    queue.add(slot=1, sender="+441", signal_ts=2, attachment="/tmp/b.ogg")
    queue.mark_played(kept)

    assert queue.prune(keep_days=30) == 0  # nothing is old enough yet
    assert queue.total_pending() == 1
