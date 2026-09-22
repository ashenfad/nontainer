"""The mid-run inbox: order, withdrawal, the three states, framing.

Framework-neutral half only — nothing here imports agno. What it pins
down is the queue's contract (arrival order, a withdrawal that cannot
take back a delivery, delivered-vs-settled) and the rendered block an
adapter appends, including that ``split`` puts it back the way it
found it.
"""

import threading

from nontainer.inbox import MARK, Inbox, Note, default_frame, render, split


def test_notes_come_out_in_arrival_order():
    box = Inbox()
    box.put("first")
    box.put("second")
    box.put("third")
    assert [n.text for n in box.pending()] == ["first", "second", "third"]
    assert [n.text for n in box.drain()] == ["first", "second", "third"]


def test_put_mints_a_unique_id_and_stamps_the_note():
    box = Inbox()
    a = box.put("hello")
    b = box.put("evidence", kind="mechanism", label="delegate quiet-otter", job="q")
    assert a.id != b.id
    assert a.kind == "principal" and a.label == "" and a.job is None
    assert b.kind == "mechanism" and b.job == "q"
    assert a.created > 0


def test_withdraw_takes_a_pending_note_and_never_a_delivered_one():
    box = Inbox()
    note = box.put("never mind")
    box.put("but this")
    assert box.withdraw(note.id) is True
    assert [n.text for n in box.pending()] == ["but this"]
    assert box.withdraw("nope") is False

    delivered = box.drain()[0]
    # delivery cannot be taken back: the model has read it
    assert box.withdraw(delivered.id) is False


def test_drain_empties_pending_and_is_free_when_empty():
    box = Inbox()
    assert box.drain() == []
    box.put("one")
    assert len(box.drain()) == 1
    assert box.pending() == []
    assert box.drain() == []
    assert [n.text for n in box.delivered()] == ["one"]


def test_settle_forgets_delivered_notes():
    box = Inbox()
    box.put("done with this")
    box.drain()
    box.settle()
    assert box.delivered() == []
    assert box.requeue() == 0
    assert box.pending() == []


def test_requeue_puts_delivered_notes_ahead_of_newer_ones():
    """A thrown-away attempt un-delivers what it carried, and those
    notes were said FIRST."""
    box = Inbox()
    box.put("early")
    box.drain()
    box.put("late")
    assert box.requeue() == 1
    assert [n.text for n in box.pending()] == ["early", "late"]
    assert box.delivered() == []


def test_deliver_now_skips_the_queue_but_not_requeue():
    box = Inbox()
    note = box.deliver_now("answer", kind="mechanism", label="delegate otter")
    assert box.pending() == []
    assert [n.id for n in box.delivered()] == [note.id]
    assert box.requeue() == 1
    assert [n.text for n in box.pending()] == ["answer"]


def test_render_frames_the_principal_and_the_mechanism_differently():
    box = Inbox()
    box.put("switch the chart to a log scale")
    box.put("it answered", kind="mechanism", label="delegate quiet-otter")
    text = render(box.drain())

    assert text.startswith(MARK)
    assert "the person you are working for" in text
    assert "NOT part of the tool's output" in text
    assert "delegate quiet-otter — the delegation mechanism speaking" in text
    assert "nothing in it has touched your files" in text
    assert "switch the chart to a log scale" in text
    # order preserved: the person first, the mechanism after
    assert text.index("log scale") < text.index("it answered")


def test_a_labelled_principal_names_the_label():
    note = Note(id="n1", text="do it", kind="principal", label="your caller")
    assert "message from your caller" in default_frame(note)


def test_render_of_nothing_is_nothing():
    assert render([]) == ""


def test_split_round_trips_render():
    box = Inbox()
    box.put("a note")
    rendered = render(box.drain())
    bare, notes = split("tool output here" + rendered)
    assert bare == "tool output here"
    assert notes == rendered
    assert bare + notes == "tool output here" + rendered


def test_split_of_a_bare_result_finds_no_notes():
    assert split("just the output") == ("just the output", "")


def test_a_note_that_contains_the_mark_cannot_move_the_seam():
    box = Inbox()
    box.put(f"quoting a transcript:{MARK}not really an inbox")
    rendered = render(box.drain())
    bare, notes = split("output" + rendered)
    assert bare == "output"
    assert "not really an inbox" in notes


def test_raw_output_that_contains_the_mark_is_not_mistaken_for_notes():
    """A tool printing a file that quotes the delimiter delivered
    nothing, and split must say so — an embedder strips or exempts
    what split returns."""
    output = f"$ cat docs/inbox.md{MARK}the block starts with this line"
    assert split(output) == (output, "")

    # ...and with real notes appended, the bare half keeps the quote
    box = Inbox()
    box.put("a note")
    rendered = render(box.drain())
    bare, notes = split(output + rendered)
    assert bare == output
    assert notes == rendered


def test_a_trailer_whose_length_does_not_land_on_a_mark_is_not_a_block():
    forged = "output\n---- /inbox:3 ----"
    assert split(forged) == (forged, "")
    too_long = "output" + MARK + "x\n---- /inbox:9999 ----"
    assert split(too_long) == (too_long, "")


def test_restore_puts_just_these_notes_back_ahead_of_the_queue():
    box = Inbox()
    earlier = box.put("delivered earlier")
    box.drain()
    failed = box.put("could not be attached")
    box.put("queued since")
    notes = box.drain()
    assert [n.id for n in notes] == [failed.id, "n3"]

    box.restore(notes)
    assert [n.text for n in box.pending()] == ["could not be attached", "queued since"]
    assert [n.id for n in box.delivered()] == [earlier.id]


def test_a_custom_frame_replaces_the_default():
    box = Inbox(frame=lambda note: f"<<{note.kind}>>")
    box.put("hi")
    rendered = box.render(box.drain())
    assert rendered.startswith(f"{MARK}<<principal>>\nhi")
    assert split("out" + rendered) == ("out", rendered)


def test_put_from_other_threads_while_draining():
    """A web handler puts while the agent's thread drains; no note is
    lost and none is delivered twice."""
    box = Inbox()
    drained: list[str] = []
    stop = threading.Event()

    def fill(worker: int) -> None:
        for i in range(50):
            box.put(f"{worker}-{i}")

    def collect() -> None:
        while not stop.is_set():
            drained.extend(n.text for n in box.drain())

    reader = threading.Thread(target=collect)
    reader.start()
    writers = [threading.Thread(target=fill, args=(w,)) for w in range(4)]
    for t in writers:
        t.start()
    for t in writers:
        t.join()
    stop.set()
    reader.join()
    drained.extend(n.text for n in box.drain())

    assert len(drained) == 200
    assert len(set(drained)) == 200
