import asyncio
import random
import time

from .config import RecoveryConfig


class Clock:
    def monotonic(self):
        return time.monotonic()

    def wall(self):
        return time.time()

    async def sleep(self, seconds):
        await asyncio.sleep(seconds)


def delay(config: RecoveryConfig, attempt_number: int, random_value: float) -> float:
    # Clamp the exponent before evaluating it, even for adversarial stored counters.
    nominal = min(config.backoff_cap_s,
                  config.backoff_base_s * config.backoff_multiplier ** min(100, max(0, attempt_number - 1)))
    return min(config.backoff_cap_s, nominal * (1 + random_value * config.jitter_fraction))


def schedule(config, event, attempt_number, *, now_mono, now_wall, global_next, random_value=None):
    local = delay(config, attempt_number, random.random() if random_value is None else random_value)
    return max(event.first_seen_mono + config.stable_error_s,
               event.first_seen_mono + config.builtin_retry_grace_s,
               event.first_seen_mono + local,
               event.first_seen_mono + event.retry_after_s,
               now_mono + max(0, global_next - now_wall))

