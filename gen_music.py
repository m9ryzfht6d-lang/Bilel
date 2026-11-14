#!/usr/bin/env python3
"""Original background music for the reels (no samples, no copyright issues).

Warm cinematic piano + pad, then a soft chill beat after a riser and an impact.
96 BPM (one beat = 0.625 s, one bar = 2.5 s), D major, progression Bm - G - D - A.
The video editor cuts on the same beat grid, so cuts land on the music.

Usage:
    python3 gen_music.py OUT.wav --intro 4 --main 8 --outro 1
Sections are in bars. The riser plays during the last intro bar when a main
section follows; the impact lands on the first beat of the main section.
"""
import argparse
import wave

import numpy as np
from scipy.signal import butter, fftconvolve, sosfilt

SR = 48000
BPM = 96
BEAT = 60.0 / BPM
BAR = 4 * BEAT
rng = np.random.default_rng(2026)


def mtof(m):
    return 440.0 * 2.0 ** ((m - 69) / 12.0)


# pad voicing, piano arpeggio notes, bass note (MIDI)
CHORDS = {
    "Bm":   ([59, 62, 66, 69], [71, 74, 78, 81], 47),
    "G":    ([59, 62, 66, 67], [71, 74, 78, 79], 43),
    "D":    ([57, 62, 64, 66], [69, 74, 76, 78], 38),
    "Asus": ([57, 62, 64, 69], [69, 74, 76, 81], 45),
    "A":    ([57, 61, 64, 69], [69, 73, 76, 81], 45),
    "Dend": ([57, 62, 66, 69, 74], [62, 66, 69, 74, 78], 38),
}
PROGRESSION = ["Bm", "G", "D", "A"]


def lp(x, fc, order=2):
    return sosfilt(butter(order, fc, "low", fs=SR, output="sos"), x, axis=0)


def hp(x, fc, order=2):
    return sosfilt(butter(order, fc, "high", fs=SR, output="sos"), x, axis=0)


def bp(x, lo, hi, order=2):
    return sosfilt(butter(order, [lo, hi], "band", fs=SR, output="sos"), x, axis=0)


def add(buf, sig, start):
    i = int(round(start * SR))
    if i >= len(buf):
        return
    n = min(len(sig), len(buf) - i)
    buf[i:i + n] += sig[:n]


def pan(mono, p):
    """p in -1..1 -> stereo (equal power)."""
    a = (p + 1) * np.pi / 4
    return np.stack([mono * np.cos(a), mono * np.sin(a)], axis=1)


# ---------------------------------------------------------------- voices
def piano_note(m, vel, dur=3.0, damp=None):
    """damp: seconds after which the damper falls (short release), like a
    real piano when the key is let go; None = let it ring."""
    f = mtof(m)
    t = np.arange(int(dur * SR)) / SR
    out = np.zeros_like(t)
    B = 0.00025
    for k in range(1, 11):
        fk = k * f * np.sqrt(1 + B * k * k)
        if fk > 11000:
            break
        ak = (1.0 / k ** 1.9) * (vel ** (0.45 * (k - 1)))
        tau = 1.4 / (1 + 0.45 * (k - 1)) * (440.0 / f) ** 0.35
        out += ak * np.exp(-t / tau) * np.sin(2 * np.pi * fk * t + rng.uniform(0, 2 * np.pi))
    out *= 1 - np.exp(-t / 0.0025)                      # hammer attack
    hammer = lp(rng.standard_normal(len(t)), 2500) * np.exp(-t / 0.004) * 0.05
    out += hammer
    if damp is not None:
        i = int(damp * SR)
        out[i:] *= np.exp(-(t[i:] - damp) / 0.08)
    out[-int(0.05 * SR):] *= np.linspace(1, 0, int(0.05 * SR))
    return out * vel


def pad_chord(notes, dur, attack, release):
    n = int((dur + release) * SR)
    t = np.arange(n) / SR
    out = np.zeros((n, 2))
    for m in notes:
        f0 = mtof(m)
        for ch in range(2):
            for cents in (-7, 0, 7):
                f = f0 * 2 ** ((cents + rng.uniform(-1.5, 1.5)) / 1200)
                kmax = int(min(20, 4500 / f))
                for k in range(1, kmax + 1):
                    out[:, ch] += (1.0 / k) * np.sin(2 * np.pi * k * f * t + rng.uniform(0, 2 * np.pi))
    env = np.minimum(1, t / attack)
    rel = t > dur
    env[rel] *= np.exp(-(t[rel] - dur) / (release / 4))
    env = env * env * (3 - 2 * env)                     # smooth curve
    return out * env[:, None] / (len(notes) * 3 * 3)


def bass_note(m, dur):
    f = mtof(m)
    t = np.arange(int((dur + 0.3) * SR)) / SR
    x = np.sin(2 * np.pi * f * t) + 0.35 * np.sin(4 * np.pi * f * t) + 0.12 * np.sin(6 * np.pi * f * t)
    x = np.tanh(1.6 * x) / np.tanh(1.6)
    x += 0.45 * np.sin(np.pi * f * t)                   # sub octave
    env = np.minimum(1, t / 0.03)
    rel = t > dur
    env[rel] *= np.exp(-(t[rel] - dur) / 0.07)
    return x * env


def kick():
    t = np.arange(int(0.45 * SR)) / SR
    f = 46 + 110 * np.exp(-t / 0.03)
    ph = 2 * np.pi * np.cumsum(f) / SR
    x = np.sin(ph) * np.exp(-t / 0.22)
    x += 0.25 * np.sin(2 * ph) * np.exp(-t / 0.06)      # audible on phones
    x += lp(rng.standard_normal(len(t)), 4000) * np.exp(-t / 0.002) * 0.15
    return x * (1 - np.exp(-t / 0.001))


def snap():
    t = np.arange(int(0.3 * SR)) / SR
    x = np.zeros_like(t)
    for off in (0.0, 0.009, 0.018):
        i = int(off * SR)
        x[i:] += rng.standard_normal(len(t) - i) * np.exp(-(t[: len(t) - i]) / 0.05)
    return bp(x, 1100, 4200) * 0.9


def hat(vel):
    t = np.arange(int(0.08 * SR)) / SR
    return hp(rng.standard_normal(len(t)), 7500) * np.exp(-t / 0.018) * vel


def riser(dur):
    n = int(dur * SR)
    t = np.arange(n) / SR
    noise = rng.standard_normal(n)
    out = np.zeros(n)
    hop = 1024
    win = np.hanning(2 * hop)
    for i in range(0, n - 2 * hop, hop):
        c = 300 * (8000 / 300) ** ((i / n) ** 1.6)
        seg = bp(noise[i:i + 2 * hop], c * 0.7, min(c * 1.4, 20000))
        out[i:i + 2 * hop] += seg * win
    return out * (t / dur) ** 2.5


def impact():
    t = np.arange(int(2.0 * SR)) / SR
    f = 38 + 30 * np.exp(-t / 0.15)
    ph = 2 * np.pi * np.cumsum(f) / SR
    x = (np.sin(ph) + 0.35 * np.sin(2 * ph) + 0.15 * np.sin(3 * ph)) * np.exp(-t / 0.7)
    x += lp(rng.standard_normal(len(t)), 2500) * np.exp(-t / 0.18) * 0.35
    return x * (1 - np.exp(-t / 0.002))


def reverb_ir(rt60=2.4, length=3.0):
    n = int(length * SR)
    t = np.arange(n) / SR
    ir = lp(rng.standard_normal((n, 2)), 5000) * np.exp(-6.91 * t / rt60)[:, None]
    ir[: int(0.015 * SR)] = 0                           # pre-delay
    return ir / np.sqrt(np.sum(ir ** 2, axis=0))


def wet(bus, ir):
    return np.stack([fftconvolve(bus[:, c], ir[:, c])[: len(bus)] for c in range(2)], axis=1)


# ---------------------------------------------------------------- song
def build(intro, main, outro):
    bars = intro + main + outro
    total = bars * BAR + 3.0
    n = int(total * SR)
    pad = np.zeros((n, 2))
    piano = np.zeros((n, 2))
    bass = np.zeros(n)
    drums = np.zeros((n, 2))
    fx = np.zeros((n, 2))

    chords = [PROGRESSION[b % 4] for b in range(intro + main)] + ["Dend"] * outro
    if outro == 0 and chords:
        chords[-1] = "Dend"

    for b, name in enumerate(chords):
        t0 = b * BAR
        in_main = intro <= b < intro + main
        is_outro = b >= intro + main
        pv, arp, bn = CHORDS[name]

        # pad (A chord starts suspended and resolves halfway)
        att = 2.0 if b == 0 else 0.45
        if name == "A":
            add(pad, pad_chord(CHORDS["Asus"][0], BAR / 2, att, 0.6), t0)
            add(pad, pad_chord(pv, BAR / 2, 0.3, 1.0), t0 + BAR / 2)
        else:
            dur = BAR * (2.2 if is_outro else 1)
            add(pad, pad_chord(pv, dur, att, 1.0) * (0.8 if is_outro else 1), t0)

        # piano
        if is_outro:
            for i, m in enumerate(arp):
                add(piano, pan(piano_note(m, 0.62 - 0.04 * i, 4.5), -0.3 + 0.15 * i), t0 + 0.045 * i)
        elif in_main:
            pattern = [0, 2, 1, 3, 0, 2, 1, 3]
            notes = CHORDS["Asus"][1] if name == "A" else arp
            for i, idx in enumerate(pattern):
                if name == "A" and i >= 4:
                    notes = arp
                vel = (0.58 if i % 2 == 0 else 0.45) + rng.uniform(-0.05, 0.05)
                jit = rng.uniform(-0.006, 0.006)
                add(piano, pan(piano_note(notes[idx], vel, 1.6, damp=0.55), -0.35 + 0.2 * idx),
                    t0 + i * BEAT / 2 + jit)
        else:
            pattern = [0, 2, 1, 3]
            notes = CHORDS["Asus"][1] if name == "A" else arp
            for i, idx in enumerate(pattern):
                vel = (0.55 if i == 0 else 0.42) + rng.uniform(-0.04, 0.04)
                add(piano, pan(piano_note(notes[idx], vel, 3.0, damp=1.1), -0.3 + 0.2 * idx),
                    t0 + i * BEAT + (0 if (b == 0 and i == 0) else rng.uniform(-0.008, 0.008)))
            # a low root note on the downbeat for weight
            add(piano, pan(piano_note(bn + 12, 0.4, 3.0), 0.0), t0)

        # bass + drums in the main section
        if in_main:
            add(bass, bass_note(bn, BAR - 0.05), t0)
            last_bar = b == intro + main - 1
            for beat in range(4):
                tb = t0 + beat * BEAT
                if beat in (0, 2):
                    add(drums, pan(kick() * 0.95, 0), tb)
                if beat in (1, 3):
                    add(drums, pan(snap() * 0.32, 0.05), tb)
                for half in range(2):
                    add(drums, pan(hat(0.10 if half else 0.06), 0.35), tb + half * BEAT / 2)
            if b % 2 == 1 and not last_bar:
                add(drums, pan(kick() * 0.55, 0), t0 + 2.5 * BEAT)
        elif is_outro and b == intro + main:
            add(bass, bass_note(bn, BAR * 1.5), t0)

    if main and intro:
        r = riser(BAR)
        add(fx, pan(r * 0.16, -0.2) + pan(r * 0.16, 0.2), (intro - 1) * BAR)
        add(fx, pan(impact() * 0.8, 0), intro * BAR)

    # side-chain pump of pad and bass from the main-section kicks
    t = np.arange(n) / SR
    duck = np.ones(n)
    for b in range(intro, intro + main):
        for beat in (0, 2):
            tk = b * BAR + beat * BEAT
            i = int(tk * SR)
            seg = t[i:] - tk
            duck[i:] = np.minimum(duck[i:], 1 - 0.42 * np.exp(-seg / 0.17))
    pad *= duck[:, None]
    bass *= duck

    # tone shaping
    pad = hp(lp(pad, 2200), 140)
    piano = lp(piano, 5200)
    bass = lp(bass, 900)

    ir = reverb_ir()
    mix = (0.55 * pad + 0.32 * wet(pad, ir)
           + 0.50 * piano + 0.34 * wet(piano, ir)
           + 0.55 * pan(bass, 0)
           + drums + 0.18 * wet(drums, ir)
           + fx + 0.45 * wet(fx, ir))
    mix = hp(mix, 28)
    raw_peak = np.max(np.abs(mix))
    mix /= raw_peak                                     # peak 1.0 before saturation
    mix = np.tanh(mix * 1.3) / np.tanh(1.3)             # gentle glue on peaks only
    mix /= np.max(np.abs(mix)) / 0.89
    fade = int(1.5 * SR)
    mix[-fade:] *= np.linspace(1, 0, fade)[:, None]
    return mix


def write_wav(path, x):
    data = (np.clip(x, -1, 1) * 32767).astype("<i2")
    with wave.open(path, "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(data.tobytes())


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("out")
    p.add_argument("--intro", type=int, default=4)
    p.add_argument("--main", type=int, default=8)
    p.add_argument("--outro", type=int, default=1)
    a = p.parse_args()
    write_wav(a.out, build(a.intro, a.main, a.outro))
    print(f"wrote {a.out}: {(a.intro + a.main + a.outro) * BAR + 3.0:.1f}s at {BPM} BPM")
