"""Factor registry for managing and discovering alpha factors.

Provides a centralized catalog of :class:`AlphaFactor` implementations with
support for registration via method call or decorator, lookup by name, and
filtering by category.
"""

from __future__ import annotations

from typing import Callable, Dict, List, Optional, Type

from .factor_base import AlphaFactor, FactorMetadata


class FactorLibrary:
    """Registry for alpha factor classes.

    The library stores *classes* (not instances) so that factors can be
    instantiated on demand with their own configuration.

    Example::

        library = FactorLibrary()
        library.register(MyMomentumFactor)

        # Later ...
        factor_cls = library.get("momentum_12m")
    """

    _instance: Optional["FactorLibrary"] = None

    def __init__(self) -> None:
        self._factors: Dict[str, Type[AlphaFactor]] = {}
        self._metadata: Dict[str, FactorMetadata] = {}

    # ------------------------------------------------------------------
    # Singleton accessor (optional convenience)
    # ------------------------------------------------------------------

    @classmethod
    def default(cls) -> "FactorLibrary":
        """Return the global default library instance."""
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    # ------------------------------------------------------------------
    # Registration
    # ------------------------------------------------------------------

    def register(
        self,
        factor_cls: Type[AlphaFactor],
        metadata: Optional[FactorMetadata] = None,
    ) -> None:
        """Register a factor class in the library.

        If *metadata* is not provided the class must expose a
        ``default_metadata`` class method that returns a
        :class:`FactorMetadata`.

        Args:
            factor_cls: The alpha factor class to register.
            metadata: Optional metadata override.

        Raises:
            ValueError: If the factor name is already registered.
            AttributeError: If *metadata* is ``None`` and the class has no
                ``default_metadata`` class method.
        """
        if metadata is None:
            if not hasattr(factor_cls, "default_metadata"):
                raise AttributeError(
                    f"{factor_cls.__name__} must either provide metadata at "
                    "registration time or implement a 'default_metadata' "
                    "classmethod."
                )
            metadata = factor_cls.default_metadata()  # type: ignore[attr-defined]

        name = metadata.name
        if name in self._factors:
            raise ValueError(
                f"Factor '{name}' is already registered. Use a unique name."
            )

        self._factors[name] = factor_cls
        self._metadata[name] = metadata

    def get(self, name: str) -> Type[AlphaFactor]:
        """Retrieve a factor class by name.

        Args:
            name: The registered factor name.

        Returns:
            The factor class.

        Raises:
            KeyError: If no factor with *name* exists.
        """
        if name not in self._factors:
            raise KeyError(
                f"Factor '{name}' not found. Available: "
                f"{list(self._factors.keys())}"
            )
        return self._factors[name]

    def get_metadata(self, name: str) -> FactorMetadata:
        """Retrieve metadata for a registered factor.

        Args:
            name: The registered factor name.

        Returns:
            The :class:`FactorMetadata` associated with *name*.
        """
        return self._metadata[name]

    # ------------------------------------------------------------------
    # Discovery helpers
    # ------------------------------------------------------------------

    def list_factors(self) -> List[str]:
        """Return a sorted list of all registered factor names."""
        return sorted(self._factors.keys())

    def list_by_category(self, category: str) -> List[str]:
        """Return factor names filtered by category.

        Args:
            category: Category string to match (e.g. ``"momentum"``).

        Returns:
            Sorted list of matching factor names.
        """
        return sorted(
            name
            for name, meta in self._metadata.items()
            if meta.category == category
        )

    def __contains__(self, name: str) -> bool:
        return name in self._factors

    def __len__(self) -> int:
        return len(self._factors)

    def __repr__(self) -> str:
        return f"FactorLibrary(factors={len(self._factors)})"


# ----------------------------------------------------------------------
# Decorator for auto-registration
# ----------------------------------------------------------------------

def register_factor(
    metadata: Optional[FactorMetadata] = None,
    library: Optional[FactorLibrary] = None,
) -> Callable[[Type[AlphaFactor]], Type[AlphaFactor]]:
    """Class decorator that auto-registers an :class:`AlphaFactor` subclass.

    Args:
        metadata: Optional :class:`FactorMetadata`.  If ``None`` the class
            must implement a ``default_metadata`` classmethod.
        library: Target :class:`FactorLibrary` instance.  Defaults to the
            global :meth:`FactorLibrary.default` instance.

    Returns:
        The original class, unmodified.

    Example::

        @register_factor(FactorMetadata(
            name="momentum_12m",
            category="momentum",
            universe="equity",
        ))
        class Momentum12M(AlphaFactor):
            ...
    """
    target = library or FactorLibrary.default()

    def decorator(cls: Type[AlphaFactor]) -> Type[AlphaFactor]:
        target.register(cls, metadata=metadata)
        return cls

    return decorator
