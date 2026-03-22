"""Model versioning and registry.

Provides local model storage with version tracking and metadata persistence.
Designed with an integration hook for MLflow or similar experiment tracking
platforms.
"""

from __future__ import annotations

import json
import logging
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

from .model_base import MLModel

logger = logging.getLogger(__name__)


class ModelVersion:
    """Immutable record of a registered model version.

    Attributes:
        name: Model name.
        version: Integer version number.
        metrics: Evaluation metrics captured at registration time.
        created_at: UTC timestamp of registration.
        path: Filesystem path where the model artifact is stored.
        metadata: Arbitrary key-value metadata.
    """

    def __init__(
        self,
        name: str,
        version: int,
        metrics: Dict[str, float],
        created_at: str,
        path: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        self.name = name
        self.version = version
        self.metrics = metrics
        self.created_at = created_at
        self.path = path
        self.metadata = metadata or {}

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "version": self.version,
            "metrics": self.metrics,
            "created_at": self.created_at,
            "path": self.path,
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ModelVersion":
        return cls(**data)

    def __repr__(self) -> str:
        return (
            f"ModelVersion(name={self.name!r}, version={self.version}, "
            f"metrics={self.metrics})"
        )


class ModelRegistry:
    """Local model registry with version management.

    Models are stored under ``<registry_root>/<model_name>/v<version>/`` with
    a ``registry.json`` manifest that tracks all versions and their metadata.

    Parameters:
        registry_root: Root directory for model storage.  Created
            automatically if it does not exist.

    Example::

        registry = ModelRegistry("./model_store")
        registry.register_model("lgbm_alpha", model, {"mse": 0.012})
        loaded = registry.load_model("lgbm_alpha", version=1)
    """

    def __init__(self, registry_root: Union[str, Path] = "./model_registry") -> None:
        self._root = Path(registry_root)
        self._root.mkdir(parents=True, exist_ok=True)
        self._manifest_path = self._root / "registry.json"
        self._manifest: Dict[str, List[Dict[str, Any]]] = self._load_manifest()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def register_model(
        self,
        name: str,
        model: MLModel,
        metrics: Optional[Dict[str, float]] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> ModelVersion:
        """Register a trained model under the given name.

        The model is saved to a versioned directory and its metadata is
        appended to the manifest.

        Args:
            name: Logical model name (e.g. ``"lgbm_momentum"``).
            model: A fitted :class:`MLModel` instance.
            metrics: Optional evaluation metrics dictionary.
            metadata: Optional arbitrary metadata.

        Returns:
            The created :class:`ModelVersion` record.
        """
        versions = self._manifest.get(name, [])
        next_version = len(versions) + 1

        version_dir = self._root / name / f"v{next_version}"
        version_dir.mkdir(parents=True, exist_ok=True)
        artifact_path = version_dir / "model.bin"

        model.save(artifact_path)

        record = ModelVersion(
            name=name,
            version=next_version,
            metrics=metrics or {},
            created_at=datetime.now(timezone.utc).isoformat(),
            path=str(artifact_path),
            metadata=metadata or {},
        )

        versions.append(record.to_dict())
        self._manifest[name] = versions
        self._save_manifest()

        logger.info("Registered %s v%d at %s", name, next_version, artifact_path)
        return record

    def load_model(
        self,
        name: str,
        model: MLModel,
        version: Optional[int] = None,
    ) -> MLModel:
        """Load a previously registered model from disk.

        Args:
            name: Logical model name.
            model: An *uninitialized* :class:`MLModel` instance of the
                correct type.  Its :meth:`load` method will be called with
                the stored artifact path.
            version: Version number to load.  Defaults to the latest.

        Returns:
            The loaded :class:`MLModel` instance.

        Raises:
            KeyError: If *name* is not found in the registry.
            ValueError: If the requested *version* does not exist.
        """
        if name not in self._manifest:
            raise KeyError(
                f"Model '{name}' not found. Available: {self.list_models()}"
            )

        versions = self._manifest[name]
        if version is None:
            record = versions[-1]
        else:
            matches = [v for v in versions if v["version"] == version]
            if not matches:
                available = [v["version"] for v in versions]
                raise ValueError(
                    f"Version {version} not found for '{name}'. "
                    f"Available: {available}"
                )
            record = matches[0]

        model.load(record["path"])
        logger.info("Loaded %s v%d from %s", name, record["version"], record["path"])
        return model

    def list_models(self) -> List[str]:
        """Return all registered model names."""
        return sorted(self._manifest.keys())

    def list_versions(self, name: str) -> List[ModelVersion]:
        """Return all versions for a given model name.

        Args:
            name: Logical model name.

        Returns:
            List of :class:`ModelVersion` records, ordered by version.
        """
        versions = self._manifest.get(name, [])
        return [ModelVersion.from_dict(v) for v in versions]

    def get_best_version(
        self,
        name: str,
        metric: str = "mse",
        lower_is_better: bool = True,
    ) -> ModelVersion:
        """Return the version with the best value for a given metric.

        Args:
            name: Logical model name.
            metric: Metric key to compare.
            lower_is_better: If ``True``, the lowest value wins.

        Returns:
            The best :class:`ModelVersion`.
        """
        versions = self.list_versions(name)
        if not versions:
            raise KeyError(f"No versions found for '{name}'.")

        key_fn = lambda v: v.metrics.get(metric, float("inf") if lower_is_better else float("-inf"))
        return min(versions, key=key_fn) if lower_is_better else max(versions, key=key_fn)

    # ------------------------------------------------------------------
    # MLflow integration hook
    # ------------------------------------------------------------------

    def log_to_mlflow(self, name: str, version: Optional[int] = None) -> None:
        """Log a model version to MLflow (stub).

        Override this method or extend the class to integrate with MLflow
        for centralized experiment tracking.

        Args:
            name: Model name.
            version: Version to log.  Defaults to latest.
        """
        logger.warning(
            "MLflow integration is not configured. Override "
            "ModelRegistry.log_to_mlflow() to enable."
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _load_manifest(self) -> Dict[str, List[Dict[str, Any]]]:
        if self._manifest_path.exists():
            with open(self._manifest_path, "r") as f:
                return json.load(f)
        return {}

    def _save_manifest(self) -> None:
        with open(self._manifest_path, "w") as f:
            json.dump(self._manifest, f, indent=2)
