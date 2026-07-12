"""GMM-based market regime detector with walk-forward fitting.

Wraps ``sklearn.mixture.GaussianMixture`` and adds:
  * Standardization (within-fit) so different feature scales don't
    dominate the cluster geometry
  * NaN handling (drop rows with missing features before fit)
  * Walk-forward helper: at any prediction date, fit on the trailing N
    days of features and predict the current day's cluster

Usage::

    from research.regime import GMMRegimeDetector, compute_macro_features

    feats = compute_macro_features(panel)              # wide daily features
    det = GMMRegimeDetector(n_components=4)
    det.fit(feats.loc[:'2014-12-31'])
    label = det.predict_one(feats.loc['2015-01-15'])   # int cluster id
    proba = det.predict_proba_one(feats.loc['2015-01-15'])  # (k,) array
"""

from __future__ import annotations

from typing import Optional, Tuple

import numpy as np
import pandas as pd


class GMMRegimeDetector:
    """Gaussian Mixture Model regime classifier."""

    def __init__(
        self,
        n_components: int = 4,
        random_state: int = 42,
        covariance_type: str = "full",
        max_iter: int = 200,
    ):
        self.n_components = n_components
        self.random_state = random_state
        self.covariance_type = covariance_type
        self.max_iter = max_iter
        self._gmm = None
        self._mean: Optional[pd.Series] = None
        self._std: Optional[pd.Series] = None
        self._feature_names: Optional[list] = None
        self._fitted = False

    # ------------------------------------------------------------------
    # Fit / predict
    # ------------------------------------------------------------------
    def fit(self, features: pd.DataFrame) -> "GMMRegimeDetector":
        """Fit the GMM on a DataFrame of regime features.

        NaN rows are dropped before fitting.
        """
        from sklearn.mixture import GaussianMixture

        df = features.dropna()
        if len(df) < self.n_components * 10:
            raise ValueError(
                f"Need at least {self.n_components * 10} non-NaN rows; got {len(df)}"
            )

        self._feature_names = list(df.columns)
        self._mean = df.mean()
        self._std = df.std().replace(0, 1.0)
        X = ((df - self._mean) / self._std).values

        self._gmm = GaussianMixture(
            n_components=self.n_components,
            covariance_type=self.covariance_type,
            random_state=self.random_state,
            max_iter=self.max_iter,
            init_params="kmeans",
        )
        self._gmm.fit(X)
        self._fitted = True
        return self

    def _to_X(self, features: pd.DataFrame | pd.Series) -> np.ndarray:
        if isinstance(features, pd.Series):
            df = features.to_frame().T
        else:
            df = features
        df = df[self._feature_names]
        df = (df - self._mean) / self._std
        return df.values

    def predict(self, features: pd.DataFrame) -> np.ndarray:
        if not self._fitted:
            raise RuntimeError("Detector not fitted. Call fit() first.")
        X = self._to_X(features)
        return self._gmm.predict(X)

    def predict_proba(self, features: pd.DataFrame) -> np.ndarray:
        if not self._fitted:
            raise RuntimeError("Detector not fitted. Call fit() first.")
        X = self._to_X(features)
        return self._gmm.predict_proba(X)

    def predict_one(self, features_row: pd.Series) -> int:
        return int(self.predict(features_row.to_frame().T)[0])

    def predict_proba_one(self, features_row: pd.Series) -> np.ndarray:
        return self.predict_proba(features_row.to_frame().T)[0]

    # ------------------------------------------------------------------
    # Cluster centroids (in original feature space) — used by labelling
    # ------------------------------------------------------------------
    def cluster_centroids(self) -> pd.DataFrame:
        """Return the cluster means in the ORIGINAL feature space
        (un-standardized). Rows = clusters, columns = features."""
        if not self._fitted:
            raise RuntimeError("Detector not fitted.")
        means_std = self._gmm.means_  # (k, d) in standardized space
        means_orig = means_std * self._std.values + self._mean.values
        return pd.DataFrame(
            means_orig,
            columns=self._feature_names,
            index=[f"cluster_{i}" for i in range(self.n_components)],
        )

    # ------------------------------------------------------------------
    # Walk-forward helper
    # ------------------------------------------------------------------
    @classmethod
    def walk_forward_predict(
        cls,
        features: pd.DataFrame,
        train_window_days: int = 252 * 3,
        n_components: int = 4,
        refit_every_days: int = 21,
    ) -> Tuple[pd.Series, list]:
        """At each row, predict regime using a GMM fit on the trailing window.

        Refits every ``refit_every_days`` to amortize cost. Between refits,
        uses the most recent fit to predict.

        Returns:
            (regime_labels: pd.Series, fit_history: list of dicts)
              fit_history[i] = {fit_date, training_centroids_df}
        """
        clean = features.dropna().copy()
        if len(clean) < train_window_days + 1:
            raise ValueError(
                f"Need at least {train_window_days + 1} non-NaN rows; got {len(clean)}"
            )

        labels = pd.Series(index=clean.index, dtype="float64", name="regime")
        fit_history: list = []

        det: Optional[GMMRegimeDetector] = None
        last_fit_date = None

        for i, ts in enumerate(clean.index):
            if i < train_window_days:
                continue

            should_refit = (
                det is None
                or last_fit_date is None
                or (ts - last_fit_date).days >= refit_every_days
            )

            if should_refit:
                window = clean.iloc[i - train_window_days:i]
                det = cls(n_components=n_components).fit(window)
                last_fit_date = ts
                fit_history.append({
                    "fit_date": ts,
                    "centroids": det.cluster_centroids(),
                })

            labels.iloc[i] = det.predict_one(clean.iloc[i])

        return labels.dropna(), fit_history
