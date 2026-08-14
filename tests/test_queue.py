import unittest

from types import SimpleNamespace

from voicelink.queue import Queue


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


if __name__ == "__main__":
    unittest.main()
