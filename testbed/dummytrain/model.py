"""Logistic regression, binary cross entropy, and SGD without array libraries."""
import json
import math
from pathlib import Path
import random


class LogisticRegression:
    def __init__(self, seed=7):
        rng = random.Random(seed)
        self.weights = [rng.uniform(-0.05, 0.05) for _ in range(2)]
        self.bias = 0.0

    def score(self, x):
        return sum(w * v for w, v in zip(self.weights, x)) + self.bias

    def forward(self, x):
        z = self.score(x)
        if z >= 0:
            return 1 / (1 + math.exp(-z))
        ez = math.exp(z)
        return ez / (1 + ez)

    def loss(self, rows):
        # Stable softplus(z) - y*z.
        return sum(max(self.score(x), 0) - y * self.score(x)
                   + math.log1p(math.exp(-abs(self.score(x)))) for x, y in rows) / len(rows)

    def grad(self, rows):
        dw = [0.0, 0.0]
        db = 0.0
        for x, y in rows:
            error = self.forward(x) - y
            for j in range(2):
                dw[j] += error * x[j] / len(rows)
            db += error / len(rows)
        return dw, db

    def step(self, rows, lr):
        dw, db = self.grad(rows)
        self.weights = [w - lr * g for w, g in zip(self.weights, dw)]
        self.bias -= lr * db

    def state_dict(self):
        return {"weights": self.weights[:], "bias": self.bias}

    def save(self, path):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.state_dict(), sort_keys=True) + "\n")

    @classmethod
    def load(cls, path):
        state = json.loads(Path(path).read_text())
        model = cls()
        model.weights = list(state["weights"])
        model.bias = state["bias"]
        return model
