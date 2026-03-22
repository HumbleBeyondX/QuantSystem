"""Transformer encoder model for time-series prediction.

Implements a Transformer encoder architecture with positional encoding,
suitable for financial time-series forecasting.  Conforms to the
:class:`MLModel` interface.
"""

from __future__ import annotations

import logging
import math
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
            "PyTorch is required for TransformerPredictor. "
            "Install it with: pip install torch"
        )


# ------------------------------------------------------------------
# Building blocks
# ------------------------------------------------------------------

if _HAS_TORCH:

    class PositionalEncoding(nn.Module):
        """Sinusoidal positional encoding for sequence models.

        Args:
            d_model: Embedding dimension.
            max_len: Maximum sequence length supported.
            dropout: Dropout probability applied after the encoding is added.
        """

        def __init__(
            self,
            d_model: int,
            max_len: int = 5000,
            dropout: float = 0.1,
        ) -> None:
            super().__init__()
            self.dropout = nn.Dropout(p=dropout)

            pe = torch.zeros(max_len, d_model)
            position = torch.arange(0, max_len, dtype=torch.float).unsqueeze(1)
            div_term = torch.exp(
                torch.arange(0, d_model, 2).float()
                * (-math.log(10000.0) / d_model)
            )
            pe[:, 0::2] = torch.sin(position * div_term)
            pe[:, 1::2] = torch.cos(position * div_term)
            pe = pe.unsqueeze(0)  # (1, max_len, d_model)
            self.register_buffer("pe", pe)

        def forward(self, x: torch.Tensor) -> torch.Tensor:  # type: ignore[override]
            """Add positional encoding to input embeddings.

            Args:
                x: Tensor of shape ``(batch, seq_len, d_model)``.

            Returns:
                Tensor of the same shape with positional encoding added.
            """
            x = x + self.pe[:, : x.size(1), :]
            return self.dropout(x)

    class TransformerBlock(nn.Module):
        """Single Transformer encoder layer with pre-layer-norm.

        Args:
            d_model: Model dimension.
            nhead: Number of attention heads.
            dim_feedforward: Feed-forward hidden dimension.
            dropout: Dropout probability.
        """

        def __init__(
            self,
            d_model: int,
            nhead: int,
            dim_feedforward: int = 256,
            dropout: float = 0.1,
        ) -> None:
            super().__init__()
            self.attn = nn.MultiheadAttention(
                d_model, nhead, dropout=dropout, batch_first=True
            )
            self.ff = nn.Sequential(
                nn.Linear(d_model, dim_feedforward),
                nn.GELU(),
                nn.Dropout(dropout),
                nn.Linear(dim_feedforward, d_model),
                nn.Dropout(dropout),
            )
            self.norm1 = nn.LayerNorm(d_model)
            self.norm2 = nn.LayerNorm(d_model)

        def forward(self, x: torch.Tensor) -> torch.Tensor:  # type: ignore[override]
            # Pre-norm Transformer
            normed = self.norm1(x)
            x = x + self.attn(normed, normed, normed, need_weights=False)[0]
            x = x + self.ff(self.norm2(x))
            return x

    class _TransformerEncoder(nn.Module):
        """Full Transformer encoder for regression."""

        def __init__(
            self,
            input_size: int,
            d_model: int,
            nhead: int,
            num_layers: int,
            dim_feedforward: int = 256,
            dropout: float = 0.1,
        ) -> None:
            super().__init__()
            self.input_proj = nn.Linear(input_size, d_model)
            self.pos_enc = PositionalEncoding(d_model, dropout=dropout)
            self.layers = nn.ModuleList(
                [
                    TransformerBlock(d_model, nhead, dim_feedforward, dropout)
                    for _ in range(num_layers)
                ]
            )
            self.norm = nn.LayerNorm(d_model)
            self.head = nn.Linear(d_model, 1)

        def forward(self, x: torch.Tensor) -> torch.Tensor:  # type: ignore[override]
            """Forward pass.

            Args:
                x: ``(batch, seq_len, input_size)``

            Returns:
                ``(batch,)`` predictions.
            """
            x = self.input_proj(x)
            x = self.pos_enc(x)
            for layer in self.layers:
                x = layer(x)
            x = self.norm(x)
            # Use the last time-step representation
            out = self.head(x[:, -1, :])
            return out.squeeze(-1)


# ------------------------------------------------------------------
# MLModel wrapper
# ------------------------------------------------------------------


class TransformerPredictor(MLModel):
    """Transformer encoder predictor for time-series forecasting.

    Parameters:
        input_size: Number of features per time step.
        d_model: Transformer model dimension.
        nhead: Number of attention heads.
        num_layers: Number of Transformer encoder layers.
        dim_feedforward: Hidden dimension in the feed-forward network.
        dropout: Dropout probability.
        learning_rate: Optimizer learning rate.
        batch_size: Training mini-batch size.
        epochs: Maximum number of training epochs.
        device: ``"cpu"`` or ``"cuda"``.

    Raises:
        ImportError: If PyTorch is not installed.
    """

    def __init__(
        self,
        input_size: int = 10,
        d_model: int = 64,
        nhead: int = 4,
        num_layers: int = 2,
        dim_feedforward: int = 256,
        dropout: float = 0.1,
        learning_rate: float = 1e-3,
        batch_size: int = 64,
        epochs: int = 100,
        device: Optional[str] = None,
    ) -> None:
        _check_torch()

        self._input_size = input_size
        self._d_model = d_model
        self._nhead = nhead
        self._num_layers = num_layers
        self._dim_feedforward = dim_feedforward
        self._dropout = dropout
        self._lr = learning_rate
        self._batch_size = batch_size
        self._epochs = epochs
        self._device = torch.device(
            device or ("cuda" if torch.cuda.is_available() else "cpu")
        )

        self._model: Optional[_TransformerEncoder] = None

    # ------------------------------------------------------------------
    # MLModel interface
    # ------------------------------------------------------------------

    @property
    def name(self) -> str:
        return "TransformerPredictor"

    def fit(
        self,
        X: Union[np.ndarray, pd.DataFrame],
        y: Union[np.ndarray, pd.Series],
    ) -> "TransformerPredictor":
        """Train the Transformer model.

        Args:
            X: Feature tensor ``(n_samples, seq_len, input_size)``.
                2-D input is reshaped to ``(n_samples, 1, n_features)``.
            y: Target vector ``(n_samples,)``.

        Returns:
            ``self``.
        """
        X_arr = np.asarray(X, dtype=np.float32)
        y_arr = np.asarray(y, dtype=np.float32)

        if X_arr.ndim == 2:
            X_arr = X_arr[:, np.newaxis, :]

        self._input_size = X_arr.shape[2]

        self._model = _TransformerEncoder(
            input_size=self._input_size,
            d_model=self._d_model,
            nhead=self._nhead,
            num_layers=self._num_layers,
            dim_feedforward=self._dim_feedforward,
            dropout=self._dropout,
        ).to(self._device)

        dataset = TensorDataset(
            torch.from_numpy(X_arr), torch.from_numpy(y_arr)
        )
        loader = DataLoader(
            dataset, batch_size=self._batch_size, shuffle=True
        )

        optimizer = torch.optim.Adam(self._model.parameters(), lr=self._lr)
        scheduler = torch.optim.lr_scheduler.CosineAnnealingLR(
            optimizer, T_max=self._epochs
        )
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
                nn.utils.clip_grad_norm_(self._model.parameters(), max_norm=1.0)
                optimizer.step()
                epoch_loss += loss.item() * X_batch.size(0)

            scheduler.step()
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
        """Evaluate model performance.

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
        """Save model weights."""
        if self._model is None:
            raise RuntimeError("No model to save.")
        torch.save(
            {
                "state_dict": self._model.state_dict(),
                "config": {
                    "input_size": self._input_size,
                    "d_model": self._d_model,
                    "nhead": self._nhead,
                    "num_layers": self._num_layers,
                    "dim_feedforward": self._dim_feedforward,
                    "dropout": self._dropout,
                },
            },
            str(path),
        )
        logger.info("Model saved to %s", path)

    def load(self, path: Union[str, Path]) -> "TransformerPredictor":
        """Load model weights."""
        checkpoint = torch.load(str(path), map_location=self._device)
        cfg = checkpoint["config"]
        self._input_size = cfg["input_size"]
        self._d_model = cfg["d_model"]
        self._nhead = cfg["nhead"]
        self._num_layers = cfg["num_layers"]
        self._dim_feedforward = cfg["dim_feedforward"]
        self._dropout = cfg["dropout"]

        self._model = _TransformerEncoder(**cfg).to(self._device)
        self._model.load_state_dict(checkpoint["state_dict"])
        logger.info("Model loaded from %s", path)
        return self

    def get_feature_importance(self) -> Optional[pd.Series]:
        """Transformer does not natively provide feature importance.

        Returns:
            ``None``.
        """
        return None
