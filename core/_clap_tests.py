"""Synthetic checks for core/clap.py.  Run:  python -m core._clap_tests

Each case is a few seconds of made-up audio at 16 kHz with a quiet room under
it. Real claps differ from these, so the live check is still a pair of hands;
what this guards is the logic — the pattern, the refusals, the timing, and that
none of it depends on how loud a particular microphone is."""
from __future__ import annotations

import numpy as np

from core.clap import RATE, ClapDetector, learn_level

rng = np.random.default_rng(7)


def room(seconds: float, level: float = 0.004) -> np.ndarray:
    return rng.normal(0, level, int(RATE * seconds)).astype(np.float32)


def clap(amp: float = 0.5) -> np.ndarray:
    n = int(RATE * 0.06)
    t = np.arange(n) / RATE
    burst = rng.normal(0, 1, n) * np.exp(-t / 0.012)
    burst = np.diff(burst, prepend=0.0)
    return (amp * burst / np.max(np.abs(burst))).astype(np.float32)


def syllable(amp: float = 0.3, dur: float = 0.25) -> np.ndarray:
    t = np.arange(int(RATE * dur)) / RATE
    env = np.minimum(1, t / 0.03) * np.minimum(1, (dur - t) / 0.05)
    voice = sum(np.sin(2 * np.pi * f * t) / k for k, f in enumerate((140, 280, 420, 700), 1))
    return (amp * env * voice / 2).astype(np.float32)


def knock(amp: float = 0.6) -> np.ndarray:
    n = int(RATE * 0.08)
    t = np.arange(n) / RATE
    return (amp * np.sin(2 * np.pi * 120 * t) * np.exp(-t / 0.02)).astype(np.float32)


def seq(*parts) -> np.ndarray:
    return np.concatenate([p if isinstance(p, np.ndarray) else room(p) for p in parts])


def scaled(audio: np.ndarray, gain: float, hiss: float) -> np.ndarray:
    """The same scene on another microphone: its gain, and its own hiss."""
    return (audio * gain + rng.normal(0, hiss, audio.size)).astype(np.float32)


def fires(audio: np.ndarray, claps: int = 2, **kw) -> int:
    d = ClapDetector(claps=claps, **kw)
    count = 0
    for i in range(0, audio.size, 512):              # 32 ms callbacks
        if d.feed(audio[i:i + 512]):
            count += 1
    return count


def learned_level():
    d = ClapDetector(learn=True)
    audio = seq(2.0, *[x for _ in range(3) for x in (clap(), room(0.35), clap(), room(1.2))])
    for i in range(0, audio.size, 512):
        d.feed(audio[i:i + 512])
    return learn_level(d.heard_levels)


CASES = [
    ("double clap",                       seq(2.0, clap(), 0.35, clap(), 1.0), 1),
    ("double clap, softer",               seq(2.0, clap(0.2), 0.4, clap(0.2), 1.0), 1),
    ("single clap (needs two)",           seq(2.0, clap(), 1.5), 0),
    ("claps too far apart",               seq(2.0, clap(), 1.6, clap(), 1.0), 0),
    ("slow double clap (0.85 s)",         seq(2.0, clap(), 0.79, clap(), 1.0), 1),
    ("applause (five claps)",             seq(2.0, *[x for _ in range(5) for x in (clap(), room(0.25))], 1.0), 0),
    ("speech",                            seq(2.0, *[x for _ in range(8) for x in (syllable(), room(0.08))], 1.0), 0),
    ("one knock",                         seq(2.0, knock(), 1.5), 0),
    ("a long sound (music, a held vowel)", seq(2.0, syllable(0.4, 0.8), 0.4, syllable(0.4, 0.8), 1.0), 0),
    ("two claps, then two more later",    seq(2.0, clap(), 0.3, clap(), 4.0, clap(), 0.3, clap(), 1.0), 2),
    # What once started it by itself: sharp sounds in the middle of talk.
    ("two sharp sounds inside talk",      seq(2.0, syllable(), 0.05, clap(0.3), syllable(0.3, 0.2), clap(0.3), syllable(), 1.0), 0),
    ("sharp sounds between words",        seq(2.0, *[x for _ in range(3) for x in (syllable(0.3, 0.2), room(0.06), clap(0.25), room(0.08))], 1.0), 0),
    ("two claps straight after a word",   seq(2.0, syllable(0.3, 0.3), clap(), 0.35, clap(), 1.0), 0),
    ("two claps, then talking at once",   seq(2.0, clap(), 0.35, clap(), 0.12, syllable(), syllable(), 1.0), 0),
]


def main() -> int:
    results = []

    def check(name, got, want):
        results.append(got == want)
        print(f"  {'PASS' if got == want else 'FAIL'}  {name:46s} fired {got}, expected {want}")

    for name, audio, want in CASES:
        check(name, fires(audio), want)
    check("single clap with claps=1", fires(seq(2.0, clap(), 1.5), claps=1), 1)

    # Every microphone: the same double clap, heard very quietly and very loudly.
    double = seq(2.0, clap(), 0.35, clap(), 1.0)
    check("quiet webcam (x0.12 gain)", fires(scaled(double, 0.12, 0.0004)), 1)
    check("hot laptop microphone (x1.8 gain, loud hiss)",
          fires(np.clip(scaled(double, 1.8, 0.01), -1, 1)), 1)
    check("nothing but the microphone's hiss", fires(room(4.0, 0.002)), 0)
    check("low sensitivity still hears a clear double clap",
          fires(double, sensitivity=0.7), 1)

    # Learning this microphone's clap shuts out quieter sharp sounds.
    level = learned_level()
    results.append(level is not None)
    print(f"  {'PASS' if level else 'FAIL'}  {'learning mode hears the claps':46s} level {level}")
    if level:
        clicks = seq(2.0, clap(0.1), 0.35, clap(0.1), 1.0)
        check("two soft clicks in a quiet room, learned", fires(clicks, level=level), 0)
        check("real claps, learned", fires(double, level=level), 1)

    bad = results.count(False)
    print(f"\n{len(results) - bad} passed, {bad} failed")
    return bad


if __name__ == "__main__":
    raise SystemExit(main())
