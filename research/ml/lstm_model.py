"""LSTM model for sequential return prediction.

Wraps a PyTorch LSTM network behind the :class:`MLModel` interface, providing
configurable architecture, DataLoader-based training, and inference with
``torch.no_grad``.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

import numpy as np
import pandas as pd

try:
    import torch
    import torch.nn as nn
    from torch.utils.data import DataLoader, TensorDataset

    _HAS_TORCH = True
except ImportError:
    _HAS_TORCH = False

from .model_base import MLModel

logger = logging.getLogger(__name__)


def _check_torch() -> None:
    if not _HAS_TORCH:
        raise ImportError(
            "PyTorch is required for LSTMPredictor. "
            "Install it with: pip install torch"
        )


# ------------------------------------------------------------------
# LSTM network definition
# ------------------------------------------------------------------

if _HAS_TORCH:

    class _LSTMNetwork(nn.Module):
        """Multi-layer LSTM followed by a linear projection head."""

        def __init__(
            self,
            input_size: int,
            hidden_size: int,
            num_layers: int,
            dropout: float,
        ) -> None:
            super().__init__()
            self.lstm = nn.LSTM(
                input_size=input_size,
                hidden_size=hidden_size,
                num_layers=num_layers,
                dropout=dropout if num_layers > 1 else 0.0,
                batch_first=True,
            )
            self.fc = nn.Linear(hidden_size, 1)
            self.dropout = nn.Dropout(dropout)

        def forward(self, x: torch.Tensor) -> torch.Tensor:  # type: ignore[override]
            """Forward pass.

            Args:
                x: Input tensor of shape ``(batch, seq_len, input_size)``.

            Returns:
                Predictions of shape ``(batch,)``.
            """
            lstm_out, _ = self.lstm(x)
            last_hidden = lstm_out[:, -1, :]
            out = self.fc(self.dropout(last_hidden))
            return out.squeeze(-1)


# ------------------------------------------------------------------
# MLModel wrapper
# ------------------------------------------------------------------


class LSTMPredictor(MLModel):
    """LSTM-based predictor for time-series return forecasting.

    Parameters:
        input_size: Number of features per time step.
        hidden_size: LSTM hidden dimension.
        num_layers: Number of stacked LSTM layers.
        dropout: Dropout probability applied between LSTM layers and before
            the output projection.
        learning_rate: Adam optimizer learning rate.
        batch_size: Training mini-batch size.
        epochs: Maximum number of training epochs.
        device: ``"cpu"`` or ``"cuda"``.  Defaults to CUDA if available.

    Raises:
        ImportError: If PyTorch is not installed.
    """

    def __init__(
        self,
        input_size: int = 10,
        hidden_size: int = 64,
        num_layers: int = 2,
        dropout: float = 0.2,
        learning_rate: float = 1e-3,
        batch_size: int = 64,
        epochs: int = 100,
        device: Optional[str] = None,
    ) -> None:
        _check_torch()

        self._input_size = input_size
        self._hidden_size = hidden_size
        self._num_layers = num_layers
        self._dropout = dropout
        self._lr = learning_rate
        self._batch_size = batch_size
        self._epochs = epochs
        self._device = torch.device(
            device or ("cuda" if torch.cuda.is_available() else "cpu")
        )

        self._model: Optional[_LSTMNetwork] = None
        self._feature_names: Optional[List[str]] = None

    # ------------------------------------------------------------------
    # MLModel interface
    # ------------------------------------------------------------------

    @property
    def name(self) -> str:
        return "LSTMPredictor"

    def fit(
        self,
        X: Union[np.ndarray, pd.DataFrame],
        y: Union[np.ndarray, pd.Series],
    ) -> "LSTMPredictor":
        """Train the LSTM model.

        Args:
            X: Feature tensor of shape ``(n_samples, seq_len, input_size)``.
                If a 2-D array is provided it is reshaped to
                ``(n_samples, 1, n_features)``.
            y: Target vector ``(n_samples,)``.

        Returns:
            ``self``.
        """
        X_arr = np.asarray(X, dtype=np.float32)
        y_arr = np.asarray(y, dtype=np.float32)

        if X_arr.ndim == 2:
            X_arr = X_arr[:, np.newaxis, :]

        self._input_size = X_arr.shape[2]

        self._model = _LSTMNetwork(
            input_size=self._input_size,
            hidden_size=self._hidden_size,
            num_layers=self._num_layers,
            dropout=self._dropout,
        ).to(self._device)

        dataset = TensorDataset(
            torch.from_numpy(X_arr), torch.from_numpy(y_arr)
        )
        loader = DataLoader(
            dataset, batch_size=self._batch_size, shuffle=True
        )

        optimizer = torch.optim.Adam(self._model.parameters(), lr=self._lr)
        criterion = nn.MSELoss()

        self._model.train()
        for epoch in range(1, self._epochs + 1):
            epoch_loss = 0.0
            for X_batch, y_batch in loader:
                X_batch = X_batch.to(self._device)
                y_batch = y_batch.to(self._device)

                optimizer.zero_grad()
                preds = self._model(X_batch)
                loss = criterion(preds, y_batch)
                loss.backward()
                optimizer.step()
                epoch_loss += loss.item() * X_batch.size(0)

            avg_loss = epoch_loss / len(dataset)
            if epoch % 10 == 0 or epoch == 1:
                logger.info("Epoch %d/%d  loss=%.6f", epoch, self._epochs, avg_loss)

        return self

    def predict(self, X: Union[np.ndarray, pd.DataFrame]) -> np.ndarray:
        """Generate predictions.

        Args:
            X: Feature tensor.

        Returns:
            1-D array of predictions.
        """
        if self._model is None:
            raise RuntimeError("Model has not been fitted. Call fit() first.")

        X_arr = np.asarray(X, dtype=np.float32)
        if X_arr.ndim == 2:
            X_arr = X_arr[:, np.newaxis, :]

        self._model.eval()
        with torch.no_grad():
            tensor = torch.from_numpy(X_arr).to(self._device)
            preds = self._model(tensor).cpu().numpy()
        return preds

    def evaluate(
        self,
        X: Union[np.ndarray, pd.DataFrame],
        y: Union[np.ndarray, pd.Series],
    ) -> Dict[str, float]:
        """Evaluate model performance (MSE, RMSE, MAE, R2).

        Args:
            X: Feature tensor.
            y: Ground-truth targets.

        Returns:
            Dictionary of metric names to values.
        """
        preds = self.predict(X)
        y_arr = np.asarray(y, dtype=np.float32)

        mse = float(np.mean((preds - y_arr) ** 2))
        mae = float(np.mean(np.abs(preds - y_arr)))
        ss_res = float(np.sum((y_arr - preds) ** 2))
        ss_tot = float(np.sum((y_arr - y_arr.mean()) ** 2))
        r2 = 1.0 - ss_res / ss_tot if ss_tot > 0 else 0.0

        return {"mse": mse, "rmse": float(np.sqrt(mse)), "mae": mae, "r2": r2}

    def save(self, path: Union[str, Path]) -> None:
        """Save model weights to disk.

        Args:
            path: Destination file path.
        """
        if self._model is None:
            raise RuntimeError("No model to save.")
        torch.save(
            {
                "state_dict": self._model.state_dict(),
                "config": {
                    "input_size": self._input_size,
                    "hidden_size": self._hidden_size,
                    "num_layers": self._num_layers,
                    "dropout": self._dropout,
                },
            },
            str(path),
        )
        logger.info("Model saved to %s", path)

    def load(self, path: Union[str, Path]) -> "LSTMPredictor":
        """Load model weights from disk.

        Args:
            path: Source file path.

        Returns:
            ``self``.
        """
        checkpoint = torch.load(str(path), map_location=self._device)
        cfg = checkpoint["config"]
        self._input_size = cfg["input_size"]
        self._hidden_size = cfg["hidden_size"]
        self._num_layers = cfg["num_layers"]
        self._dropout = cfg["dropout"]

        self._model = _LSTMNetwork(**cfg).to(self._device)
        self._model.load_state_dict(checkpoint["state_dict"])
        logger.info("Model loaded from %s", path)
        return self

    def get_feature_importance(self) -> Optional[pd.Series]:
        """LSTM does not natively provide feature importance.

        Returns:
            ``None``.
        """
        return None
