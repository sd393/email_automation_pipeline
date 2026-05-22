"""Tests for scripts.lib.rate_limit."""

from __future__ import annotations

from scripts.lib.rate_limit import HourlyLimiter, RateLimiter


def test_token_bucket_2_per_sec(fake_clock):
    rl = RateLimiter(rate_per_sec=2.0, burst=1, clock=fake_clock.now, sleep=fake_clock.sleep)
    for _ in range(4):
        rl.acquire()
    # burst=1 means first acquire is immediate; remaining 3 require 1/rate seconds each.
    assert 1.4 <= fake_clock.t <= 1.6


def test_token_bucket_with_burst(fake_clock):
    rl = RateLimiter(rate_per_sec=1.0, burst=5, clock=fake_clock.now, sleep=fake_clock.sleep)
    for _ in range(5):
        rl.acquire()
    # Five tokens already in the bucket → all acquire immediately.
    assert fake_clock.t == 0.0
    # 6th acquire waits 1s for one refill.
    rl.acquire()
    assert 0.9 <= fake_clock.t <= 1.1


def test_hourly_first_three_immediate_then_blocks(fake_clock):
    hl = HourlyLimiter(
        per_hour=3, burst=3, clock=fake_clock.now, sleep=fake_clock.sleep
    )
    for _ in range(3):
        hl.acquire()
    assert fake_clock.t == 0.0
    hl.acquire()  # 4th must wait ~3600s for the first entry to age out
    assert fake_clock.t >= 3600.0


def test_hourly_sustained_rate(fake_clock):
    """Issue #12: sustained-rate behavior across the window."""
    hl = HourlyLimiter(per_hour=30, burst=5, clock=fake_clock.now, sleep=fake_clock.sleep)
    for _ in range(60):
        hl.acquire()
    # 60 acquires at 30/hr cap → at least ~30 of them are in the second window
    # (since burst=5 packed at start). Expect total time to be ≥ 1 hour.
    assert fake_clock.t >= 3600.0


def test_mixed_limiters_compose(fake_clock):
    rl = RateLimiter(rate_per_sec=10.0, burst=1, clock=fake_clock.now, sleep=fake_clock.sleep)
    hl = HourlyLimiter(per_hour=5, burst=5, clock=fake_clock.now, sleep=fake_clock.sleep)
    for _ in range(5):
        rl.acquire()
        hl.acquire()
    # First 5 inside hourly cap; rate-limited at 10/sec → ~0.4s for 4 refills.
    assert fake_clock.t < 1.0
    rl.acquire()
    hl.acquire()
    # 6th forces hourly limiter to wait ~1 hour.
    assert fake_clock.t >= 3600.0


def test_rate_limiter_recovers_after_long_pause(fake_clock):
    rl = RateLimiter(rate_per_sec=1.0, burst=3, clock=fake_clock.now, sleep=fake_clock.sleep)
    rl.acquire()
    rl.acquire()
    rl.acquire()
    # Empty bucket; fast-forward a lot and we should refill to full burst, not above.
    fake_clock.sleep(10_000.0)
    rl.acquire()
    rl.acquire()
    rl.acquire()
    # Three more immediate acquires (clock did not move during the three acquires).
    # The 4th in this batch needs 1s.
    before = fake_clock.t
    rl.acquire()
    assert fake_clock.t - before == 1.0
