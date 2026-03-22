"""Abstract base class for machine learning models.

Defines the common interface that all ML model implementations (LightGBM,
LSTM, Transformer, etc.) must follow within the research module.
"""

from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Dict, Optional, Union

import numpy as np
import pandas as pd


class MLModel(ABC):
    """Base class for all ML models in the research pipeline.

    Subclasses must implement the core lifecycle methods: fit, predict,
    evaluate, save/load, and feature importance extraction.
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """Return the human-readable model name."""
        ...

    @abstractmethod
    def fit(
        self,
        X: Union[np.ndarray, pd.DataFrame],
        y: Union[np.ndarray, pd.Series],
    ) -> "MLModel":
        """Train the model on the given data.

        Args:
            X: Feature matrix of shape ``(n_samples, n_features)`` or
                ``(n_samples, seq_len, n_features)`` for sequence models.
            y: Target values of shape ``(n_samples,)``.

        Returns:
            ``self`` for method chaining.
        """
        ...

    @abstractmethod
    def predict(
        self,
        X: Union[np.ndarray, pd.DataFrame],
    ) -> np.ndarray:
        """Generate predictions for the given features.

        Args:
            X: Feature matrix with the same schema as in :meth:`fit`.

        Returns:
            Predicted values as a 1-D numpy array.
        """
        ...

    @abstractmethod
    def evaluate(
        self,
        X: Union[np.ndarray, pd.DataFrame],
        y: Union[np.ndarray, pd.Series],
    ) -> Dict[str, float]:
        """Evaluate the model against ground-truth targets.

        Args:
            X: Feature matrix.
            y: Ground-truth target values.

        Returns:
            Dictionary of metric names to values (e.g.
            ``{"mse": 0.01, "r2": 0.85}``).
        """
        ...

    @abstractmethod
    def save(self, path: Union[str, Path]) -> None:
        """Persist the trained model to disk.

        Args:
            path: File or directory path for the saved model.
        """
        ...

    @abstractmethod
    def load(self, path: Union[str, Path]) -> "MLModel":
        """Load a previously saved model from disk.

        Args:
            path: File or directory path of the saved model.

        Returns:
            ``self`` for method chaining.
        """
        ...

    @abstractmethod
    def get_feature_importance(self) -> Optional[pd.Series]:
        """Return feature importance scores, if available.

        Returns:
            A :class:`pd.Series` mapping feature names to importance scores,
            or ``None`` if the model does not support feature importance.
        """
        ...
