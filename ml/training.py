"""ml/training.py — ML training pipeline for signal prediction."""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

try:
    from sklearn.linear_model import LogisticRegression
    from sklearn.metrics import accuracy_score, f1_score, precision_score, recall_score, roc_auc_score
    SKLEARN_AVAILABLE = True
except ImportError:
    SKLEARN_AVAILABLE = False


@dataclass
class ModelMetrics:
    accuracy: float
    precision: float
    recall: float
    f1_score: float
    auc_roc: float
    feature_importance: Dict[str, float]


@dataclass
class ModelConfig:
    model_type: str = "logistic_regression"
    learning_rate: float = 0.01
    max_iterations: int = 1000
    regularization: float = 0.01
    test_split: float = 0.2
    random_seed: int = 42


class SimpleLogisticRegression:
    """Minimal logistic regression for binary classification.

    Used when scikit-learn is not available.
    Implements gradient descent with L2 regularization.
    """

    def __init__(self, config: ModelConfig) -> None:
        self._config = config
        self._weights: List[float] = []
        self._bias: float = 0.0
        self._feature_names: List[str] = []
        self._trained: bool = False

    def fit(self, X: List[Dict[str, float]], y: List[float]) -> None:
        if not X or not y:
            raise ValueError("Empty training data")
        if len(X) != len(y):
            raise ValueError("X and y must have same length")

        self._feature_names = list(X[0].keys())
        n_features = len(self._feature_names)
        self._weights = [0.0] * n_features
        self._bias = 0.0

        X_matrix = [self._dict_to_vector(x) for x in X]
        y_vector = y

        lr = self._config.learning_rate
        reg = self._config.regularization
        max_iter = self._config.max_iterations

        for iteration in range(max_iter):
            gradients_w = [0.0] * n_features
            gradient_b = 0.0
            n = len(X_matrix)

            for i in range(n):
                pred = self._sigmoid(self._dot(X_matrix[i], self._weights) + self._bias)
                error = pred - y_vector[i]
                for j in range(n_features):
                    gradients_w[j] += error * X_matrix[i][j]
                gradient_b += error

            for j in range(n_features):
                gradients_w[j] = gradients_w[j] / n + reg * self._weights[j]
            gradient_b /= n

            for j in range(n_features):
                self._weights[j] -= lr * gradients_w[j]
            self._bias -= lr * gradient_b

        self._trained = True
        logger.info("Model trained: %d features, %d samples, %d iterations", n_features, n, max_iter)

    def predict_proba(self, X: List[Dict[str, float]]) -> List[float]:
        if not self._trained:
            raise RuntimeError("Model not trained yet")
        results = []
        for x in X:
            vec = self._dict_to_vector(x)
            prob = self._sigmoid(self._dot(vec, self._weights) + self._bias)
            results.append(prob)
        return results

    def predict(self, X: List[Dict[str, float]], threshold: float = 0.5) -> List[int]:
        probas = self.predict_proba(X)
        return [1 if p >= threshold else 0 for p in probas]

    def get_feature_importance(self) -> Dict[str, float]:
        if not self._trained:
            return {}
        total = sum(abs(w) for w in self._weights)
        if total < 1e-9:
            return {name: 0.0 for name in self._feature_names}
        return {
            name: abs(w) / total
            for name, w in zip(self._feature_names, self._weights)
        }

    def _dict_to_vector(self, x: Dict[str, float]) -> List[float]:
        return [x.get(name, 0.0) for name in self._feature_names]

    def _sigmoid(self, z: float) -> float:
        if z >= 0:
            return 1.0 / (1.0 + math.exp(-z))
        exp_z = math.exp(z)
        return exp_z / (1.0 + exp_z)

    def _dot(self, a: List[float], b: List[float]) -> float:
        return sum(x * y for x, y in zip(a, b))


class SklearnLogisticRegression:
    """Wrapper around scikit-learn's LogisticRegression."""

    def __init__(self, config: ModelConfig) -> None:
        self._config = config
        self._model = LogisticRegression(
            C=1.0 / max(config.regularization, 1e-8),
            max_iter=config.max_iterations,
            random_state=config.random_seed,
            solver="lbfgs",
        )
        self._feature_names: List[str] = []
        self._trained: bool = False

    def fit(self, X: List[Dict[str, float]], y: List[float]) -> None:
        if not X or not y:
            raise ValueError("Empty training data")
        self._feature_names = list(X[0].keys())
        X_matrix = [self._dict_to_vector(x) for x in X]
        self._model.fit(X_matrix, y)
        self._trained = True
        logger.info("sklearn model trained: %d features, %d samples", len(self._feature_names), len(X))

    def predict_proba(self, X: List[Dict[str, float]]) -> List[float]:
        if not self._trained:
            raise RuntimeError("Model not trained yet")
        X_matrix = [self._dict_to_vector(x) for x in X]
        probas = self._model.predict_proba(X_matrix)
        return [p[1] for p in probas]

    def predict(self, X: List[Dict[str, float]], threshold: float = 0.5) -> List[int]:
        probas = self.predict_proba(X)
        return [1 if p >= threshold else 0 for p in probas]

    def get_feature_importance(self) -> Dict[str, float]:
        if not self._trained:
            return {}
        coefs = self._model.coef_[0]
        total = sum(abs(c) for c in coefs)
        if total < 1e-9:
            return {name: 0.0 for name in self._feature_names}
        return {name: abs(c) / total for name, c in zip(self._feature_names, coefs)}

    def _dict_to_vector(self, x: Dict[str, float]) -> List[float]:
        return [x.get(name, 0.0) for name in self._feature_names]


class TrainingPipeline:
    """Orchestrates model training, validation, and evaluation.

    Automatically uses scikit-learn when available, falls back to
    custom logistic regression otherwise.
    """

    def __init__(self, config: ModelConfig = ModelConfig()) -> None:
        self._config = config
        self._model = None
        self._metrics: Optional[ModelMetrics] = None

    def train(self, X: List[Dict[str, float]], y: List[float]) -> ModelMetrics:
        if len(X) < 10:
            raise ValueError("Need at least 10 samples for training")

        split_idx = int(len(X) * (1 - self._config.test_split))
        X_train, X_test = X[:split_idx], X[split_idx:]
        y_train, y_test = y[:split_idx], y[split_idx:]

        if SKLEARN_AVAILABLE:
            self._model = SklearnLogisticRegression(self._config)
        else:
            self._model = SimpleLogisticRegression(self._config)

        self._model.fit(X_train, y_train)

        y_pred_proba = self._model.predict_proba(X_test)
        y_pred = self._model.predict(X_test)
        self._metrics = self._compute_metrics(y_test, y_pred, y_pred_proba)

        logger.info(
            "Training complete: accuracy=%.3f, f1=%.3f, auc=%.3f (sklearn=%s)",
            self._metrics.accuracy, self._metrics.f1_score, self._metrics.auc_roc,
            SKLEARN_AVAILABLE,
        )
        return self._metrics

    def predict(self, X: List[Dict[str, float]]) -> List[float]:
        if self._model is None:
            raise RuntimeError("Model not trained yet")
        return self._model.predict_proba(X)

    @property
    def metrics(self) -> Optional[ModelMetrics]:
        return self._metrics

    @property
    def feature_importance(self) -> Dict[str, float]:
        if self._model is None:
            return {}
        return self._model.get_feature_importance()

    def _compute_metrics(
        self, y_true: List[float], y_pred: List[int], y_proba: List[float]
    ) -> ModelMetrics:
        if SKLEARN_AVAILABLE:
            accuracy = accuracy_score(y_true, y_pred)
            precision = precision_score(y_true, y_pred, zero_division=0)
            recall = recall_score(y_true, y_pred, zero_division=0)
            f1 = f1_score(y_true, y_pred, zero_division=0)
            try:
                auc = roc_auc_score(y_true, y_proba)
            except ValueError:
                auc = 0.5
        else:
            tp = sum(1 for yt, yp in zip(y_true, y_pred) if yt == 1 and yp == 1)
            fp = sum(1 for yt, yp in zip(y_true, y_pred) if yt == 0 and yp == 1)
            fn = sum(1 for yt, yp in zip(y_true, y_pred) if yt == 1 and yp == 0)
            tn = sum(1 for yt, yp in zip(y_true, y_pred) if yt == 0 and yp == 0)

            accuracy = (tp + tn) / (tp + fp + fn + tn) if (tp + fp + fn + tn) > 0 else 0.0
            precision = tp / (tp + fp) if (tp + fp) > 0 else 0.0
            recall = tp / (tp + fn) if (tp + fn) > 0 else 0.0
            f1 = 2 * precision * recall / (precision + recall) if (precision + recall) > 0 else 0.0
            auc = self._compute_auc(y_true, y_proba)

        feature_importance = self._model.get_feature_importance() if self._model else {}

        return ModelMetrics(
            accuracy=accuracy,
            precision=precision,
            recall=recall,
            f1_score=f1,
            auc_roc=auc,
            feature_importance=feature_importance,
        )

    def _compute_auc(self, y_true: List[float], y_proba: List[float]) -> float:
        pairs = sorted(zip(y_proba, y_true), reverse=True)
        n_pos = sum(1 for _, y in pairs if y == 1)
        n_neg = sum(1 for _, y in pairs if y == 0)
        if n_pos == 0 or n_neg == 0:
            return 0.5

        auc = 0.0
        tp = 0
        fp = 0
        prev_fpr = 0.0
        prev_tpr = 0.0

        for prob, label in pairs:
            if label == 1:
                tp += 1
            else:
                fp += 1
            tpr = tp / n_pos
            fpr = fp / n_neg
            auc += (fpr - prev_fpr) * (tpr + prev_tpr) / 2
            prev_fpr = fpr
            prev_tpr = tpr

        return max(0.0, min(1.0, auc))
