import unittest

from types import SimpleNamespace

from voicelink.queue import FairQueue, Queue


def make_track(name: str, *, is_autoplay: bool = False) -> SimpleNamespace:
    return SimpleNamespace(
        uri=f"https://example.com/{name}",
        title=name,
        requester=SimpleNamespace(bot=is_autoplay),
        is_autoplay=is_autoplay,
    )


class QueueAutoplayOrderingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.queue = Queue(size=100, allow_duplicate=True, get_msg=lambda key: key)

    def test_user_track_jumps_ahead_of_pending_autoplay(self) -> None:
        for name in ("auto1", "auto2", "auto3"):
            self.queue.put(make_track(name, is_autoplay=True))
        playing = self.queue.get()
        self.assertEqual(playing.title, "auto1")

        self.queue.put(make_track("user1"))

        self.assertEqual(self.queue.get().title, "user1")
        self.assertEqual(self.queue.get().title, "auto2")
        self.assertEqual(self.queue.get().title, "auto3")

    def test_user_tracks_keep_fifo_order_among_themselves(self) -> None:
        for name in ("auto1", "auto2"):
            self.queue.put(make_track(name, is_autoplay=True))
        self.queue.get()

        self.queue.put(make_track("user1"))
        self.queue.put(make_track("user2"))

        titles = [track.title for track in self.queue.tracks()]
        self.assertEqual(titles, ["user1", "user2", "auto2"])

    def test_playlist_added_during_autoplay_preserves_its_order(self) -> None:
        for name in ("auto1", "auto2"):
            self.queue.put(make_track(name, is_autoplay=True))
        self.queue.get()

        for name in ("pl1", "pl2", "pl3"):
            self.queue.put(make_track(name))

        titles = [track.title for track in self.queue.tracks()]
        self.assertEqual(titles, ["pl1", "pl2", "pl3", "auto2"])

    def test_autoplay_track_appends_to_the_end(self) -> None:
        self.queue.put(make_track("user1"))
        self.queue.put(make_track("auto1", is_autoplay=True))
        self.queue.put(make_track("user2"))

        titles = [track.title for track in self.queue.tracks()]
        self.assertEqual(titles, ["user1", "user2", "auto1"])

    def test_put_returns_pending_position(self) -> None:
        self.assertEqual(self.queue.put(make_track("user1")), 1)
        self.assertEqual(self.queue.put(make_track("auto1", is_autoplay=True)), 2)
        self.assertEqual(self.queue.put(make_track("user2")), 2)

    def test_plain_queue_without_autoplay_behaves_as_before(self) -> None:
        self.queue.put(make_track("user1"))
        self.queue.put(make_track("user2"))
        self.assertEqual(self.queue.get().title, "user1")
        self.assertEqual(self.queue.get().title, "user2")
        self.assertTrue(self.queue.is_empty)

    def test_track_without_flag_attribute_is_treated_as_user_track(self) -> None:
        bare = SimpleNamespace(uri="https://example.com/bare", title="bare", requester=SimpleNamespace(bot=False))
        self.queue.put(make_track("auto1", is_autoplay=True))
        self.queue.put(bare)
        titles = [track.title for track in self.queue.tracks()]
        self.assertEqual(titles, ["bare", "auto1"])

    def test_new_track_never_leapfrogs_users_after_shuffle_interleaves_autoplay(self) -> None:
        self.queue.put(make_track("current"))
        self.queue.get()
        for name, auto in (("a1", True), ("u1", False), ("u2", False), ("a2", True)):
            self.queue._queue.append(make_track(name, is_autoplay=auto))

        self.queue.put(make_track("new"))

        titles = [track.title for track in self.queue.tracks()]
        self.assertEqual(titles, ["a1", "u1", "u2", "new", "a2"])


class QueueRemoveByIndexTests(unittest.TestCase):
    """A song re-added after playing exists twice; index ops must never touch the history copy."""

    def setUp(self) -> None:
        self.queue = Queue(size=100, allow_duplicate=True, get_msg=lambda key: key)

    def _played_then_readded(self):
        first = make_track("songX")
        self.queue.put(first)
        self.queue.put(make_track("songY"))
        self.queue.get()
        self.queue.get()
        readded = make_track("songX")
        self.queue.put(readded)
        return first, readded

    def test_remove_deletes_the_upcoming_copy_not_the_history_copy(self) -> None:
        first, readded = self._played_then_readded()

        removed = self.queue.remove(1)

        self.assertEqual(list(removed.values()), [readded])
        self.assertIs(self.queue._queue[0], first)
        self.assertEqual([t.title for t in self.queue.history()], ["songX"])
        self.assertTrue(self.queue.is_empty)

    def test_move_relocates_the_upcoming_copy_not_the_history_copy(self) -> None:
        first, readded = self._played_then_readded()
        self.queue.put(make_track("songZ"))

        moved = self.queue.move(1, 2)

        self.assertIs(moved, readded)
        self.assertIs(self.queue._queue[0], first)
        self.assertEqual([t.title for t in self.queue.tracks()], ["songZ", "songX"])


class FakeUser:
    def __init__(self, name: str, bot: bool = False) -> None:
        self.name = name
        self.bot = bot


class FairQueueAutoplayTests(unittest.TestCase):
    def setUp(self) -> None:
        self.queue = FairQueue(size=100, allow_duplicate=True, get_msg=lambda key: key)
        self.alice = FakeUser("alice")
        self.bob = FakeUser("bob")

    def _track(self, name, requester, *, is_autoplay=False):
        track = make_track(name, is_autoplay=is_autoplay)
        track.requester = requester
        return track

    def test_user_track_goes_ahead_of_pending_autoplay(self) -> None:
        for name in ("auto1", "auto2", "auto3"):
            self.queue.put(self._track(name, FakeUser("bot", bot=True), is_autoplay=True))
        self.queue.get()

        self.queue.put(self._track("user1", self.alice))

        self.assertEqual([t.title for t in self.queue.tracks()], ["user1", "auto2", "auto3"])

    def test_fairness_between_listeners_is_preserved(self) -> None:
        self.queue.put(self._track("a1", self.alice))
        self.queue.get()
        self.queue.put(self._track("a2", self.alice))
        self.queue.put(self._track("auto1", FakeUser("bot", bot=True), is_autoplay=True))

        self.queue.put(self._track("b1", self.bob))

        # Round-robin: alice already has a1 playing, so bob's first track
        # interleaves ahead of alice's second — and autoplay stays last.
        titles = [t.title for t in self.queue.tracks()]
        self.assertEqual(titles, ["b1", "a2", "auto1"])

    def test_autoplay_track_always_appends(self) -> None:
        self.queue.put(self._track("a1", self.alice))
        self.queue.get()
        self.queue.put(self._track("a2", self.alice))

        self.queue.put(self._track("auto1", FakeUser("bot", bot=True), is_autoplay=True))

        self.assertEqual([t.title for t in self.queue.tracks()], ["a2", "auto1"])


if __name__ == "__main__":
    unittest.main()
