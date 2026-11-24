import logging
import random
from datetime import UTC, datetime, time, timedelta
from uuid import NAMESPACE_URL, uuid4, uuid5

from .events import DEVICES, Event
from .settings import utc_today

log = logging.getLogger(__name__)
FILMS = ("aurora", "the-last-train", "deep-blue", "paper-moon", "summer-stories", "the-passage")


def playback_session(rng, user, film, start, session_id=None):
    """Return an ordered series with seconds of wall time and playback position."""
    session_id = session_id or str(uuid4())
    device = rng.choice(DEVICES)
    steps = []
    if rng.random() < 0.55:
        steps.append(("SEARCHED", -8, 0))
    steps.append(("VIEW_STARTED", 0, 0))
    elapsed_pause = 0
    if rng.random() < 0.65:
        position = rng.randint(60, 1200)
        elapsed_pause = rng.randint(15, 240)
        steps.extend((
            ("VIEW_PAUSED", position, position),
            ("VIEW_RESUMED", position + elapsed_pause, position),
        ))
    if rng.random() < 0.75:
        duration = rng.randint(3600, 7200)
        steps.append(("VIEW_FINISHED", duration + elapsed_pause, duration))
        if rng.random() < 0.35:
            steps.append(("LIKED", duration + elapsed_pause + 7, 0))

    return [Event(
        event_id=uuid5(NAMESPACE_URL, f"{session_id}/{kind}"), user_id=user, movie_id=film,
        event_type=kind, timestamp=start + timedelta(seconds=elapsed), device_type=device,
        session_id=session_id, progress_seconds=progress,
    ) for kind, elapsed, progress in steps]


def history(today):
    for age in range(10, 0, -1):
        cohort = today - timedelta(days=age)
        for number in range(8):
            user = f"demo-{cohort}-{number}"
            for offset in range(min(age, 8)):
                day = cohort + timedelta(days=offset)
                rng = random.Random(f"{user}/{day}")
                if offset and rng.random() > 0.7 / offset**0.55:
                    continue
                start = datetime.combine(day, time(9, rng.randrange(60)), tzinfo=UTC)
                session_id = str(uuid5(NAMESPACE_URL, f"movie-analytics/{user}/{day}"))
                yield playback_session(rng, user, rng.choice(FILMS), start, session_id)


def generate(publish, stop, interval):
    for session in history(utc_today()):
        while not stop.is_set():
            try:
                for event in session:
                    if stop.is_set():
                        return
                    publish(event)
                break
            except Exception:
                log.exception("Historical event generation failed, retrying session")
                stop.wait(interval)
        if stop.is_set():
            return

    rng = random.Random()
    while not stop.is_set():
        start = datetime.now(UTC) - timedelta(hours=2, minutes=5)
        cohort = start.date() - timedelta(days=rng.randrange(8))
        user = f"demo-{cohort}-{rng.randrange(8)}"
        try:
            for event in playback_session(rng, user, rng.choice(FILMS), start):
                if stop.is_set():
                    return
                publish(event)
        except Exception:
            log.exception("Live event generation failed")
        stop.wait(interval)
