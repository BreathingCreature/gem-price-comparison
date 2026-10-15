import threading

from core import events


def test_emit_without_a_listener_does_nothing():
    assert events.emit("anything", x=1) is None
    assert events.snapshot() == []


def test_capture_collects_events_in_order():
    seen = []
    with events.capture(seen.append):
        events.emit("first", a=1)
        events.emit("second", b=2)

    assert [e["type"] for e in seen] == ["first", "second"]
    assert [e["seq"] for e in seen] == [1, 2]
    assert seen[0]["a"] == 1 and seen[1]["b"] == 2
    assert seen[1]["t"] >= seen[0]["t"] >= 0


def test_snapshot_returns_what_was_emitted_so_far():
    with events.capture():
        events.emit("one")
        events.emit("two")
        assert [e["type"] for e in events.snapshot()] == ["one", "two"]


def test_a_broken_listener_cannot_break_the_run():
    def explode(_event):
        raise RuntimeError("listener bug")

    with events.capture(explode):
        event = events.emit("still_fine")

    assert event["type"] == "still_fine"


def test_listener_is_detached_after_the_block():
    seen = []
    with events.capture(seen.append):
        events.emit("inside")
    events.emit("outside")

    assert [e["type"] for e in seen] == ["inside"]


def test_two_threads_do_not_see_each_others_events():
    results = {}

    def work(name):
        seen = []
        with events.capture(seen.append):
            for i in range(20):
                events.emit(name, i=i)
        results[name] = seen

    threads = [threading.Thread(target=work, args=(n,)) for n in ("alpha", "beta")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    for name, seen in results.items():
        assert len(seen) == 20
        assert {e["type"] for e in seen} == {name}
