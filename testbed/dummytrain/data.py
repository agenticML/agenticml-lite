"""Seeded two-dimensional blobs with a held-out split."""
import random


def make_data(seed=7, size=160, val_fraction=0.25):
    if size < 4 or not 0 < val_fraction < 1:
        raise ValueError("need size >= 4 and 0 < val_fraction < 1")
    rng = random.Random(seed)
    rows = []
    for i in range(size):
        label = i % 2
        center = 2 * label - 1
        rows.append(((rng.gauss(center, 0.5), rng.gauss(center, 0.5)), label))
    rng.shuffle(rows)
    split = max(1, min(size - 1, int(size * (1 - val_fraction))))
    return rows[:split], rows[split:]
