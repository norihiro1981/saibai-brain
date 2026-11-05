"""デモ動画の BGM を、コードだけで作る（素材を持ち込まない＝権利の心配が無い）。

使い方：uv run python video/bgm.py 秒 出力.wav [場面の頭の秒 ...]

- 柔らかい和音の帯（pad）＋低い根音＋つま弾き（arp）。72 拍／分・和音は 2 小節ごと。
- 最初の場面（課題）は短調で暗く、2 つ目の場面の頭から長調に移り、つま弾きが入る。
- 最後の 6 秒で静かに消える。残響は ffmpeg の aecho で足す。
"""

from __future__ import annotations

import subprocess
import sys
import wave
from pathlib import Path

import numpy as np

SR = 48000
BPM = 72
BEAT = 60 / BPM
CHORD = 8 * BEAT  # 2 小節

# 和音（MIDI の番号）。短調＝課題・長調＝その後
MINOR = [[57, 60, 64, 67], [53, 57, 60, 64], [50, 53, 57, 60], [52, 56, 59, 64]]  # Am7 Fmaj7 Dm7 E
MAJOR = [
    [48, 55, 59, 64],
    [45, 52, 55, 60],
    [41, 48, 52, 57],
    [43, 50, 55, 62],
]  # Cmaj7 Am7 Fmaj7 G


def hz(n: float) -> float:
    return 440.0 * 2 ** ((n - 69) / 12)


def env(n: int, attack: float, release: float) -> np.ndarray:
    e = np.ones(n)
    a, r = int(attack * SR), int(release * SR)
    e[:a] = np.linspace(0, 1, a) ** 2
    e[-r:] *= np.linspace(1, 0, r) ** 2
    return e


def tone(f: float, n: int, harmonics: int, roll: float, detune: float = 0.0) -> np.ndarray:
    t = np.arange(n) / SR
    out = np.zeros(n)
    for h in range(1, harmonics + 1):
        out += np.sin(2 * np.pi * f * h * (1 + detune) * t + h) / h**roll
    return out


def make(total: float, marks: list[float]) -> np.ndarray:
    n = int(total * SR) + SR * 3
    L, R = np.zeros(n), np.zeros(n)
    turn = marks[1] if len(marks) > 1 else 0.0  # 長調に移る時刻
    t, k = 0.0, 0
    while t < total:
        minor = t < turn - 0.5
        chords = MINOR if minor else MAJOR
        ch = chords[k % 4]
        seg = CHORD if t + CHORD < turn or not minor else max(BEAT * 2, turn - t)
        a = int(t * SR)
        m = int((seg + 2.5) * SR)  # 次の和音へ重ねる
        m = min(m, n - a)
        e = env(m, 1.6, 2.5)
        # 和音の帯（左右で少しずらす）
        for note in ch:
            f = hz(note)
            L[a : a + m] += 0.06 * e * tone(f, m, 6, 1.6, -0.0015)
            R[a : a + m] += 0.06 * e * tone(f, m, 6, 1.6, +0.0015)
        # 低い根音
        b = 0.10 * e * tone(hz(ch[0] - 12), m, 3, 2.0)
        L[a : a + m] += b
        R[a : a + m] += b
        # つま弾き（長調の間だけ・8 分音符で和音を上る）
        if not minor:
            pat = ch + [ch[1] + 12, ch[2] + 12, ch[1] + 12, ch[3]]
            for i in range(int(seg / (BEAT / 2))):
                s = int((t + i * BEAT / 2) * SR)
                ln = min(int(1.2 * SR), n - s)
                if ln <= 0:
                    break
                tt = np.arange(ln) / SR
                f = hz(pat[i % len(pat)] + 12)
                p = (np.sin(2 * np.pi * f * tt) + 0.3 * np.sin(4 * np.pi * f * tt)) * np.exp(
                    -tt * 5
                )
                amp = 0.035 * (0.8 if i % 2 else 1.0)
                pan = 0.5 + 0.3 * np.sin(i * 0.9)
                L[s : s + ln] += amp * (1 - pan) * p
                R[s : s + ln] += amp * pan * p
        t += seg
        k += 1
        if minor and t >= turn - 0.5:
            k = 0
    out = np.stack([L, R], axis=1)[: int(total * SR)]
    # 頭は 1.5 秒で立ち上がり、最後の 6 秒で消える
    fade = np.ones(len(out))
    fi, fo = int(1.5 * SR), int(6 * SR)
    fade[:fi] = np.linspace(0, 1, fi)
    fade[-fo:] = np.linspace(1, 0, fo) ** 1.5
    out *= fade[:, None]
    return out / (np.abs(out).max() + 1e-9) * 0.7


def write(path: Path, x: np.ndarray) -> None:
    raw = path.with_suffix(".dry.wav")
    with wave.open(str(raw), "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes((x * 32767).astype("<i2").tobytes())
    subprocess.run(
        [
            "ffmpeg", "-y", "-loglevel", "error", "-i", str(raw),
            "-af", "aecho=0.8:0.6:90|170|260:0.35|0.25|0.15,lowpass=f=6000,loudnorm=I=-20",
            "-ar", str(SR), str(path),
        ],
        check=True,
    )  # fmt: skip
    raw.unlink()


if __name__ == "__main__":
    total = float(sys.argv[1])
    marks = [float(x) for x in sys.argv[3:]] or [0.0]
    write(Path(sys.argv[2]), make(total, marks))
