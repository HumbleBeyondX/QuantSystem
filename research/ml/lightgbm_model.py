"""LightGBM wrapper for factor return prediction.

Provides a scikit-learn-compatible LightGBM regressor that conforms to the
:class:`MLModel` interface, with built-in cross-validation and feature
importance extraction.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

import numpy as np
import pandas as pd

try:
    import lightgbm as lgb

    _HAS_LIGHTGBM = True
except ImportError:
    _HAS_LIGHTGBM = False

from .model_base import MLModel

logger = logging.getLogger(__name__)

_DEFAULT_PARAMS: Dict[str, Any] = {
    "objective": "regression",
    "metric": "mse",
    "boosting_type": "gbdt",
    "num_leaves": 31,
    "learning_rate": 0.05,
    "feature_fraction": 0.8,
    "bagging_fraction": 0.8,
    "bagging_freq": 5,
    "verbose": -1,
}


class LightGBMPredictor(MLModel):
    """LightGBM-based predictor for factor return forecasting.

    Parameters:
        params: LightGBM parameter dictionary.  Merged with sensible
            defaults; user-supplied keys take precedence.
        num_boost_round: Maximum number of boosting iterations.
        early_stopping_rounds: Patience for early stopping during training.
            Set to ``None`` to disable.
        n_cv_folds: Number of cross-validation folds used in :meth:`fit`
            when *use_cv* is ``True``.
        feature_names: Optional list of feature names.  If not provided they
            are inferred from the input DataFrame or auto-generated.

    Raises:
        ImportError: If LightGBM is not installed.
    """

    def __init__(
        self,
        params: Optional[Dict[str, Any]] = None,
        num_boost_round: int = 500,
        early_stopping_rounds: Optional[int] = 50,
        n_cv_folds: int = 5,
        feature_names: Optional[List[str]] = None,
    ) -> None:
        if not _HAS_LIGHTGBM:
            raise ImportError(
                "LightGBM is required for LightGBMPredictor. "
                "Install it with: pip install lightgbm"
            )

        self._params: Dict[str, Any] = {**_DEFAULT_PARAMS, **(params or {})}
        self._num_boost_round = num_boost_round
        self._early_stopping_rounds = early_stopping_rounds
        self._n_cv_folds = n_cv_folds
        self._feature_names = feature_names

        self._model: Optional[lgb.Booster] = None
        self._cv_results: Optional[Dict[str, Any]] = None

    # ------------------------------------------------------------------
    # MLModel interface
    # ------------------------------------------------------------------

    @property
    def name(self) -> str:
        return "LightGBMPredictor"

    def fit(
        self,
        X: Union[np.ndarray, pd.DataFrame],
        y: Union[np.ndarray, pd.Series],
        use_cv: bool = False,
        eval_set: Optional[tuple] = None,
    ) -> "LightGBMPredictor":
        """Train the LightGBM model.

        Args:
            X: Feature matrix ``(n_samples, n_features)``.
            y: Target vector ``(n_samples,)``.
            use_cv: If ``True``, train using k-fold cross-validation and
                store CV metrics in ``self._cv_results``.
            eval_set: Optional ``(X_val, y_val)`` tuple for early stopping.

        Returns:
            ``self``.
        """
        feature_names = self._resolve_feature_names(X)
        dtrain = lgb.Dataset(X, label=y, feature_name=feature_names)

        callbacks = []
        if self._early_stopping_rounds is not None:
            callbacks.append(
                lgb.early_stopping(self._early_stopping_rounds, verbose=False)
            )
        callbacks.append(lgb.log_evaluation(period=0))

        if use_cv:
            self._cv_results = lgb.cv(
                self._params,
                dtrain,
                num_boost_round=self._num_boost_round,
                nfold=self._n_cv_folds,
                callbacks=callbacks,
                return_cvbooster=True,
            )
            # Use the best iteration from CV to retrain on all data.
            best_iter = len(self._cv_results["valid mse-mean"])  # type: ignore[index]
            self._model = lgb.train(
                self._params,
                dtrain,
                num_boost_round=best_iter,
            )
            logger.info("CV best iteration: %d", best_iter)
        else:
            valid_sets = [dtrain]
            if eval_set is not None:
                X_val, y_val = eval_set
                dval = lgb.Dataset(X_val, label=y_val, feature_name=feature_names)
                valid_sets.append(dval)

            self._model = lgb.train(
                self._params,
                dtrain,
                num_boost_round=self._num_boost_round,
                valid_sets=valid_sets,
                callbacks=callbacks,
            )

        self._feature_names = feature_names
        return self

    def predict(self, X: Union[np.ndarray, pd.DataFrame]) -> np.ndarray:
        """Generate predictions.

        Args:
            X: Feature matrix.

        Returns:
            1-D array of predictions.

        Raises:
            RuntimeError: If the model has not been fitted.
        """
        if self._model is None:
            raise RuntimeError("Model has not been fitted. Call fit() first.")
        return self._model.predict(X)  # type: ignore[return-value]

    def evaluate(
        self,
        X: Union[np.ndarray, pd.DataFrame],
        y: Union[np.ndarray, pd.Series],
    ) -> Dict[str, float]:
        """Evaluate model performance.

        Computes MSE, RMSE, MAE, and R-squared.

        Args:
            X: Feature matrix.
            y: Ground-truth targets.

        Returns:
            Dictionary of metric names to values.
        """
        preds = self.predict(X)
        y_arr = np.asarray(y)

        mse = float(np.mean((preds - y_arr) ** 2))
        mae = float(np.mean(np.abs(preds - y_arr)))
        ss_res = np.sum((y_arr - preds) ** 2)
        ss_tot = np.sum((y_arr - y_arr.mean()) ** 2)
        r2 = float(1.0 - ss_res / ss_tot) if ss_tot > 0 else 0.0

        return {
            "mse": mse,
            "rmse": float(np.sqrt(mse)),
            "mae": mae,
            "r2": r2,
        }

    def save(self, path: Union[str, Path]) -> None:
        """Save the model to a text file.

        Args:
            path: Destination file path.
        """
        if self._model is None:
            raise RuntimeError("No model to save. Call fit() first.")
        self._model.save_model(str(path))
        logger.info("Model saved to %s", path)

    def load(self, path: Union[str, Path]) -> "LightGBMPredictor":
        """Load a model from a text file.

        Args:
            path: Source file path.

        Returns:
            ``self``.
        """
        self._model = lgb.Booster(model_file=str(path))
        logger.info("Model loaded from %s", path)
        return self

    def get_feature_importance(self) -> Optional[pd.Series]:
        """Return feature importance based on split count.

        Returns:
            Series mapping feature names to importance, or ``None``.
        """
        if self._model is None:
            return None

        importance = self._model.feature_importance(importance_type="split")
        names = self._feature_names or [
            f"f_{i}" for i in range(len(importance))
        ]
        return pd.Series(importance, index=names).sort_values(ascending=False)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _resolve_feature_names(
        self, X: Union[np.ndarray, pd.DataFrame]
    ) -> List[str]:
        if self._feature_names is not None:
            return self._feature_names
        if isinstance(X, pd.DataFrame):
            return list(X.columns.astype(str))
        return [f"f_{i}" for i in range(X.shape[1])]
