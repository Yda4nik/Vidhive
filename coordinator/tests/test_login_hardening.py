"""Login hardening: throttling, constant-ish timing, non-blocking hashing, secret key."""

import asyncio
import time

from app.core.config import resolve_secret_key
from app.services.ratelimit import LoginLimiter, login_limiter
from app.services.security import DUMMY_HASH, ahash_password, averify_password, hash_password


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


# --------------------------------------------------------------------------- #
# The limiter itself (fake clock)
# --------------------------------------------------------------------------- #
def test_limiter_blocks_after_the_limit_and_reports_the_wait():
    clk = Clock()
    lim = LoginLimiter(max_failures=3, ip_max_failures=99, window_seconds=300, clock=clk)
    for _ in range(3):
        assert lim.blocked_for("1.1.1.1", "bob") == 0
        lim.record_failure("1.1.1.1", "bob")
        clk.t += 10
    assert lim.blocked_for("1.1.1.1", "bob") == 270        # until the oldest failure ages out
    clk.t += 271
    assert lim.blocked_for("1.1.1.1", "bob") == 0           # window passed -> allowed again


def test_limiter_is_per_address_and_username():
    lim = LoginLimiter(max_failures=2, ip_max_failures=99, clock=Clock())
    for _ in range(2):
        lim.record_failure("1.1.1.1", "bob")
    assert lim.blocked_for("1.1.1.1", "bob") > 0
    assert lim.blocked_for("2.2.2.2", "bob") == 0           # the real bob elsewhere is not locked out
    assert lim.blocked_for("1.1.1.1", "alice") == 0         # other accounts from that IP still work
    assert lim.blocked_for("1.1.1.1", "  BOB ") > 0         # usernames are normalised


def test_limiter_caps_one_address_spraying_many_usernames():
    lim = LoginLimiter(max_failures=5, ip_max_failures=4, clock=Clock())
    for name in ("a", "b", "c", "d"):
        lim.record_failure("9.9.9.9", name)
    assert lim.blocked_for("9.9.9.9", "never-tried") > 0
    assert lim.blocked_for("8.8.8.8", "a") == 0


def test_limiter_success_clears_the_pair():
    lim = LoginLimiter(max_failures=2, ip_max_failures=99, clock=Clock())
    lim.record_failure("1.1.1.1", "bob")
    lim.record_success("1.1.1.1", "bob")
    lim.record_failure("1.1.1.1", "bob")
    assert lim.blocked_for("1.1.1.1", "bob") == 0           # only 1 failure counted since the success


# --------------------------------------------------------------------------- #
# Through the real login form
# --------------------------------------------------------------------------- #
def _bad(client, user="tester-admin"):
    return client.post("/login", data={"username": user, "password": "nope"})


def test_form_locks_after_repeated_failures_even_for_the_right_password(anon_client):
    for _ in range(5):
        assert _bad(anon_client).status_code == 200
    r = anon_client.post("/login", data={"username": "tester-admin", "password": "adminpass"})
    assert r.status_code == 429
    assert "Слишком много" in r.text
    assert anon_client.get("/", follow_redirects=False).status_code == 303   # not signed in


def test_a_successful_login_resets_the_counter(anon_client):
    for _ in range(4):
        _bad(anon_client)
    ok = anon_client.post("/login", data={"username": "tester-admin", "password": "adminpass"},
                          follow_redirects=False)
    assert ok.status_code == 303
    for _ in range(4):                                       # 4 more failures are tolerated again
        assert _bad(anon_client).status_code == 200


def test_throttling_is_per_username(anon_client):
    for _ in range(5):
        _bad(anon_client, "ghost")
    assert _bad(anon_client, "ghost").status_code == 429
    ok = anon_client.post("/login", data={"username": "tester-admin", "password": "adminpass"},
                          follow_redirects=False)
    assert ok.status_code == 303                             # a different account is unaffected


# --------------------------------------------------------------------------- #
# Timing / event loop
# --------------------------------------------------------------------------- #
def test_unknown_user_costs_about_as_much_as_a_wrong_password(anon_client):
    def avg(user):
        t = time.perf_counter()
        for _ in range(3):
            _bad(anon_client, user)
        return (time.perf_counter() - t) / 3

    known, unknown = avg("tester-admin"), avg("nobody-here")
    # Before the fix: ~129 ms vs ~11 ms. Now both hash, so they're the same order.
    assert unknown > known * 0.5, f"unknown {unknown*1000:.0f} ms vs known {known*1000:.0f} ms"


def test_hashing_does_not_block_the_event_loop():
    stored = hash_password("pw")

    async def scenario():
        worst = 0.0
        stop = False

        async def ticker():
            nonlocal worst
            last = time.perf_counter()
            while not stop:
                await asyncio.sleep(0.005)
                now = time.perf_counter()
                worst = max(worst, now - last - 0.005)
                last = now

        t = asyncio.create_task(ticker())
        await asyncio.sleep(0.02)
        results = await asyncio.gather(*(averify_password("pw", stored) for _ in range(4)))
        stop = True
        await t
        return results, worst

    results, worst = asyncio.run(scenario())
    assert all(results)
    # Inline, each verify freezes the loop ~100 ms; off-loop the ticker barely slips.
    assert worst < 0.06, f"event loop stalled for {worst*1000:.0f} ms"


def test_async_hash_roundtrip_and_dummy_hash_is_valid():
    async def go():
        h = await ahash_password("s3cret")
        return await averify_password("s3cret", h), await averify_password("x", DUMMY_HASH)

    ok, dummy = asyncio.run(go())
    assert ok is True and dummy is False
    assert DUMMY_HASH.startswith("pbkdf2_sha256$")


# --------------------------------------------------------------------------- #
# Session secret
# --------------------------------------------------------------------------- #
def test_missing_or_placeholder_secret_is_replaced_by_a_random_one():
    for bad in (None, "", "   ", "dev-insecure-change-me", "change-me-to-a-long-random-string", "short"):
        key, ephemeral = resolve_secret_key(bad)
        assert ephemeral is True and len(key) >= 32
    a, _ = resolve_secret_key("")
    b, _ = resolve_secret_key("")
    assert a != b                                            # not a fixed fallback


def test_a_real_secret_is_kept_as_is():
    real = "x" * 64
    assert resolve_secret_key(real) == (real, False)


def test_limiter_singleton_starts_clean_each_test(anon_client):
    assert login_limiter.blocked_for("testclient", "tester-admin") == 0
