"""Work moved onto the pool still returns its result and its errors to the caller, and runs off the calling thread."""
import threading
import unittest

from trafficgen import offload


class OffloadTests(unittest.TestCase):
    def test_the_result_is_returned(self):
        self.assertEqual(offload.offloaded(lambda a, b=0: a + b, 2, b=3), 5)

    def test_the_work_runs_on_a_pool_thread(self):
        name = offload.offloaded(lambda: threading.current_thread().name)
        self.assertNotEqual(name, "MainThread")

    def test_an_error_reaches_the_caller(self):
        with self.assertRaises(ValueError):
            offload.offloaded(int, "not a number")


if __name__ == "__main__":
    unittest.main()
