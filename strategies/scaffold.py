"""Strategy scaffold: generate new strategy files from templates.

Supports both CLI usage and programmatic API (used by Workshop backend).

CLI usage:
    python -m strategies.scaffold \\
        --name "My Crypto MM" \\
        --type hft \\
        --symbols BTC/USDT:USDT ETH/USDT:USDT \\
        --asset-class crypto_perp

    python -m strategies.scaffold --list-templates

Programmatic usage:
    from strategies.scaffold import scaffold_strategy, list_templates

    result = scaffold_strategy(
        name="My Crypto MM",
        template_type="hft",
        symbols=["BTC/USDT:USDT"],
        asset_class="crypto_perp",
    )
    # result.strategy_file, result.config_file, result.strategy_id
"""

from __future__ import annotations

import re
import textwrap
from dataclasses import dataclass, field
from pathlib import Path
from string import Template
from typing import Any, Dict, List, Optional

# ------------------------------------------------------------------
# Constants
# ------------------------------------------------------------------

_ROOT = Path(__file__).resolve().parent.parent  # QuantSystem root
_TEMPLATE_DIR = Path(__file__).resolve().parent / "templates"
_STRATEGIES_DIR = Path(__file__).resolve().parent
_CONFIG_DIR = _ROOT / "config" / "strategies"

# Template type → subdirectory for generated file
_TYPE_SUBDIRS = {
    "basic": "",       # strategies/<category>/
    "options": "options",
    "futures": "crypto",
    "hft": "crypto",
}

# Template type → default asset class
_TYPE_DEFAULTS = {
    "basic": {"asset_class": "crypto", "symbols": ["BTC/USDT"]},
    "options": {"asset_class": "options", "symbols": ["SPY"]},
    "futures": {"asset_class": "crypto_perp", "symbols": ["BTC/USDT:USDT"]},
    "hft": {"asset_class": "crypto", "symbols": ["BTC/USDT:USDT"]},
}

# Template type → default parameters for YAML config
_DEFAULT_PARAMS: Dict[str, Dict[str, Any]] = {
    "basic": {
        "lookback": 20,
        "threshold": 0.5,
    },
    "options": {
        "target_dte": 30,
        "min_vrp": 0.03,
        "max_positions": 5,
        "contracts_per_trade": 1,
        "max_portfolio_delta": 500,
        "max_portfolio_vega": 5000,
        "delta_hedge_threshold": 50,
    },
    "futures": {
        "leverage": 10.0,
        "risk_per_trade": 0.02,
        "max_margin_pct": 0.50,
        "initial_equity": 10000,
        "ema_fast": 8,
        "ema_slow": 21,
        "atr_stop_mult": 3.0,
        "atr_trail_mult": 2.0,
        "cooldown_bars": 3,
        "fee_rate": 0.0005,
    },
    "hft": {
        "imbalance_threshold": 0.3,
        "min_spread_for_entry": 3.0,
        "signal_decay_seconds": 5.0,
        "use_trade_flow": True,
        "max_inventory": 1.0,
        "inventory_skew_factor": 0.5,
        "base_spread_bps": 5.0,
        "quote_size": 0.01,
        "max_orders_per_second": 10,
    },
}


# ------------------------------------------------------------------
# Data classes
# ------------------------------------------------------------------

@dataclass
class ScaffoldResult:
    """Result of scaffolding a strategy."""
    strategy_id: str
    class_name: str
    template_type: str
    strategy_file: str  # absolute path
    config_file: str    # absolute path
    strategy_code: str  # generated source
    config_yaml: str    # generated YAML
    registered_name: str  # name in registry


@dataclass
class TemplateInfo:
    """Metadata about an available template."""
    name: str
    description: str
    base_class: str
    asset_classes: List[str]
    default_symbols: List[str]
    default_params: Dict[str, Any]
    file_path: str


# ------------------------------------------------------------------
# Template listing
# ------------------------------------------------------------------

def list_templates() -> List[TemplateInfo]:
    """List all available strategy templates with metadata."""
    templates = []

    _info = [
        TemplateInfo(
            name="basic",
            description="General-purpose bar-level strategy. Good starting point for any asset class.",
            base_class="StrategyBase",
            asset_classes=["crypto", "equity", "options", "crypto_perp", "crypto_spot"],
            default_symbols=["BTC/USDT"],
            default_params=_DEFAULT_PARAMS["basic"],
            file_path=str(_TEMPLATE_DIR / "basic.py.tmpl"),
        ),
        TemplateInfo(
            name="options",
            description="Greeks-aware options strategy with vol surface, multi-leg positions, and delta hedging.",
            base_class="OptionsStrategyBase",
            asset_classes=["options"],
            default_symbols=["SPY"],
            default_params=_DEFAULT_PARAMS["options"],
            file_path=str(_TEMPLATE_DIR / "options.py.tmpl"),
        ),
        TemplateInfo(
            name="futures",
            description="Perpetual/dated futures with leverage, funding rate, fixed-risk sizing, and ATR stops.",
            base_class="FuturesStrategyBase",
            asset_classes=["crypto_perp", "crypto"],
            default_symbols=["BTC/USDT:USDT"],
            default_params=_DEFAULT_PARAMS["futures"],
            file_path=str(_TEMPLATE_DIR / "futures.py.tmpl"),
        ),
        TemplateInfo(
            name="hft",
            description="High/medium-frequency with orderbook microstructure, inventory mgmt, and market making.",
            base_class="HFTStrategyBase",
            asset_classes=["crypto", "crypto_perp", "crypto_spot", "equity"],
            default_symbols=["BTC/USDT:USDT"],
            default_params=_DEFAULT_PARAMS["hft"],
            file_path=str(_TEMPLATE_DIR / "hft.py.tmpl"),
        ),
    ]
    return _info


def get_template(name: str) -> Optional[TemplateInfo]:
    """Get a specific template by name."""
    for t in list_templates():
        if t.name == name:
            return t
    return None


# ------------------------------------------------------------------
# Name helpers
# ------------------------------------------------------------------

def _to_strategy_id(name: str) -> str:
    """Convert display name to strategy_id: 'My Crypto MM' -> 'my_crypto_mm'."""
    s = re.sub(r'[^a-zA-Z0-9\s]', '', name)
    s = re.sub(r'\s+', '_', s.strip())
    return s.lower()


def _to_class_name(name: str) -> str:
    """Convert display name to PascalCase class: 'My Crypto MM' -> 'MyCryptoMmStrategy'."""
    words = re.sub(r'[^a-zA-Z0-9\s]', '', name).split()
    pascal = ''.join(w.capitalize() for w in words)
    if not pascal.endswith('Strategy'):
        pascal += 'Strategy'
    return pascal


def _to_filename(strategy_id: str) -> str:
    """Strategy id to module filename: 'my_crypto_mm' -> 'my_crypto_mm_strategy.py'."""
    if strategy_id.endswith('_strategy'):
        return f"{strategy_id}.py"
    return f"{strategy_id}_strategy.py"


def _to_category_dir(asset_class: str) -> str:
    """Map asset class to subdirectory: crypto_perp -> crypto, options -> options."""
    mapping = {
        "crypto": "crypto",
        "crypto_perp": "crypto",
        "crypto_spot": "crypto",
        "equity": "equity",
        "options": "options",
    }
    return mapping.get(asset_class, "crypto")


# ------------------------------------------------------------------
# YAML generation
# ------------------------------------------------------------------

def _format_yaml_symbols(symbols: List[str]) -> str:
    """Format symbols as YAML list."""
    return "\n".join(f"  - {s}" for s in symbols)


def _format_yaml_params(params: Dict[str, Any], indent: int = 2) -> str:
    """Format parameter dict as YAML with comments."""
    lines = []
    prefix = " " * indent
    for key, value in params.items():
        if isinstance(value, bool):
            lines.append(f"{prefix}{key}: {'true' if value else 'false'}")
        elif isinstance(value, float):
            lines.append(f"{prefix}{key}: {value}")
        elif isinstance(value, int):
            lines.append(f"{prefix}{key}: {value}")
        elif isinstance(value, str):
            lines.append(f"{prefix}{key}: \"{value}\"")
        else:
            lines.append(f"{prefix}{key}: {value}")
    return "\n".join(lines)


# ------------------------------------------------------------------
# Core scaffold function
# ------------------------------------------------------------------

def scaffold_strategy(
    name: str,
    template_type: str = "basic",
    symbols: Optional[List[str]] = None,
    asset_class: Optional[str] = None,
    description: Optional[str] = None,
    parameters: Optional[Dict[str, Any]] = None,
    category_dir: Optional[str] = None,
    dry_run: bool = False,
) -> ScaffoldResult:
    """Generate a new strategy from a template.

    Args:
        name: Human-readable strategy name (e.g. "My Crypto MM")
        template_type: One of: basic, options, futures, hft
        symbols: Trading symbols (defaults to template default)
        asset_class: Asset class (defaults to template default)
        description: Strategy description
        parameters: Override default parameters
        category_dir: Override auto-detected category directory
        dry_run: If True, don't write files — just return generated code

    Returns:
        ScaffoldResult with paths and generated content
    """
    # ── Resolve template ──
    tmpl_info = get_template(template_type)
    if tmpl_info is None:
        available = [t.name for t in list_templates()]
        raise ValueError(f"Unknown template: {template_type}. Available: {available}")

    template_path = Path(tmpl_info.file_path)
    if not template_path.exists():
        raise FileNotFoundError(f"Template file not found: {template_path}")

    # ── Derive names ──
    strategy_id = _to_strategy_id(name)
    class_name = _to_class_name(name)
    filename = _to_filename(strategy_id)
    desc = description or f"{name} strategy"

    # ── Resolve defaults ──
    defaults = _TYPE_DEFAULTS.get(template_type, _TYPE_DEFAULTS["basic"])
    final_symbols = symbols or defaults.get("symbols", ["BTC/USDT"])
    final_asset_class = asset_class or defaults.get("asset_class", "crypto")
    final_params = {**_DEFAULT_PARAMS.get(template_type, {})}
    if parameters:
        final_params.update(parameters)

    # ── Render strategy Python file ──
    template_src = template_path.read_text(encoding="utf-8")
    strategy_code = Template(template_src).safe_substitute(
        name=name,
        description=desc,
        strategy_id=strategy_id,
        class_name=class_name,
    )

    # ── Render YAML config ──
    config_template_path = _TEMPLATE_DIR / "config.yaml.tmpl"
    config_src = config_template_path.read_text(encoding="utf-8")
    config_yaml = Template(config_src).safe_substitute(
        name=name,
        strategy_id=strategy_id,
        asset_class=final_asset_class,
        symbols_yaml=_format_yaml_symbols(final_symbols),
        parameters_yaml=_format_yaml_params(final_params),
    )

    # ── Determine output paths ──
    cat_dir = category_dir or _to_category_dir(final_asset_class)
    strategy_dir = _STRATEGIES_DIR / cat_dir
    strategy_file = strategy_dir / filename
    config_file = _CONFIG_DIR / f"{strategy_id}.yaml"

    if not dry_run:
        # Ensure directories exist
        strategy_dir.mkdir(parents=True, exist_ok=True)
        _CONFIG_DIR.mkdir(parents=True, exist_ok=True)

        # Ensure __init__.py exists
        init_file = strategy_dir / "__init__.py"
        if not init_file.exists():
            init_file.write_text("")

        # Write files
        strategy_file.write_text(strategy_code, encoding="utf-8")
        config_file.write_text(config_yaml, encoding="utf-8")

        # Auto-add to registry discovery
        _update_registry_discovery(strategy_id, cat_dir, filename)

    return ScaffoldResult(
        strategy_id=strategy_id,
        class_name=class_name,
        template_type=template_type,
        strategy_file=str(strategy_file),
        config_file=str(config_file),
        strategy_code=strategy_code,
        config_yaml=config_yaml,
        registered_name=strategy_id,
    )


def _update_registry_discovery(strategy_id: str, category: str, filename: str) -> None:
    """Add the new strategy module to registry.py's discover_strategies()."""
    registry_path = _STRATEGIES_DIR / "registry.py"
    if not registry_path.exists():
        return

    module_path = f"strategies.{category}.{filename[:-3]}"  # strip .py

    content = registry_path.read_text(encoding="utf-8")

    # Check if already registered
    if module_path in content:
        return

    # Find the modules list and append
    marker = '    ]'  # closing bracket of modules list
    if marker in content:
        insertion = f'        "{module_path}",\n'
        content = content.replace(marker, insertion + marker, 1)
        registry_path.write_text(content, encoding="utf-8")


# ------------------------------------------------------------------
# Strategy file read/write helpers (for Workshop editor)
# ------------------------------------------------------------------

def read_strategy_file(strategy_id: str) -> Optional[str]:
    """Read a strategy's source code by strategy_id.

    Searches across all category directories.
    """
    for subdir in ["crypto", "equity", "options", ""]:
        search_dir = _STRATEGIES_DIR / subdir if subdir else _STRATEGIES_DIR
        if not search_dir.is_dir():
            continue
        for py_file in search_dir.glob("*.py"):
            content = py_file.read_text(encoding="utf-8")
            if f'register_strategy("{strategy_id}")' in content:
                return content
    return None


def find_strategy_file(strategy_id: str) -> Optional[Path]:
    """Find the file path for a strategy by its registry name."""
    for subdir in ["crypto", "equity", "options", ""]:
        search_dir = _STRATEGIES_DIR / subdir if subdir else _STRATEGIES_DIR
        if not search_dir.is_dir():
            continue
        for py_file in search_dir.glob("*.py"):
            try:
                content = py_file.read_text(encoding="utf-8")
                if f'register_strategy("{strategy_id}")' in content:
                    return py_file
            except Exception:
                continue
    return None


def write_strategy_file(strategy_id: str, code: str) -> str:
    """Write updated source code for an existing strategy.

    Returns the file path written to.
    """
    path = find_strategy_file(strategy_id)
    if path is None:
        raise FileNotFoundError(f"Strategy not found: {strategy_id}")
    path.write_text(code, encoding="utf-8")
    return str(path)


def read_strategy_config(strategy_id: str) -> Optional[str]:
    """Read a strategy's YAML config."""
    config_path = _CONFIG_DIR / f"{strategy_id}.yaml"
    if config_path.exists():
        return config_path.read_text(encoding="utf-8")
    return None


def write_strategy_config(strategy_id: str, yaml_content: str) -> str:
    """Write updated YAML config for a strategy."""
    config_path = _CONFIG_DIR / f"{strategy_id}.yaml"
    config_path.write_text(yaml_content, encoding="utf-8")
    return str(config_path)


def list_all_strategies() -> List[Dict[str, Any]]:
    """List all strategies with their metadata (for Workshop listing)."""
    results = []
    for subdir in ["crypto", "equity", "options"]:
        search_dir = _STRATEGIES_DIR / subdir
        if not search_dir.is_dir():
            continue
        for py_file in sorted(search_dir.glob("*_strategy.py")):
            content = py_file.read_text(encoding="utf-8")
            # Extract registry name
            match = re.search(r'@register_strategy\("([^"]+)"\)', content)
            if not match:
                continue
            sid = match.group(1)

            # Extract class name
            cls_match = re.search(r'class\s+(\w+)\(', content)
            cls_name = cls_match.group(1) if cls_match else sid

            # Extract base class
            base_match = re.search(r'class\s+\w+\((\w+)\)', content)
            base_cls = base_match.group(1) if base_match else "StrategyBase"

            # Extract docstring
            doc_match = re.search(r'"""(.+?)"""', content, re.DOTALL)
            doc = doc_match.group(1).strip().split('\n')[0] if doc_match else ""

            # Check if config exists
            has_config = (_CONFIG_DIR / f"{sid}.yaml").exists()

            results.append({
                "strategy_id": sid,
                "class_name": cls_name,
                "base_class": base_cls,
                "category": subdir,
                "file_path": str(py_file),
                "description": doc,
                "has_config": has_config,
                "lines": content.count('\n') + 1,
            })

    return results


# ------------------------------------------------------------------
# CLI
# ------------------------------------------------------------------

def main() -> None:
    """CLI entry point."""
    import argparse
    import json
    import sys

    parser = argparse.ArgumentParser(
        description="Scaffold a new trading strategy from a template",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent("""
        Examples:
          # List available templates
          python -m strategies.scaffold --list-templates

          # Create a crypto futures strategy
          python -m strategies.scaffold \\
            --name "BTC Trend Follower" \\
            --type futures \\
            --symbols BTC/USDT:USDT ETH/USDT:USDT

          # Create an options strategy
          python -m strategies.scaffold \\
            --name "SPY Vol Seller" \\
            --type options \\
            --symbols SPY QQQ

          # Create an HFT strategy
          python -m strategies.scaffold \\
            --name "ETH Market Maker" \\
            --type hft \\
            --symbols ETH/USDT:USDT

          # Dry run (preview without writing)
          python -m strategies.scaffold --name "Test" --type basic --dry-run

          # List all existing strategies
          python -m strategies.scaffold --list-strategies
        """),
    )

    parser.add_argument("--name", type=str, help="Strategy name (e.g. 'My Crypto MM')")
    parser.add_argument("--type", type=str, default="basic",
                        choices=["basic", "options", "futures", "hft"],
                        help="Template type (default: basic)")
    parser.add_argument("--symbols", nargs="+", help="Trading symbols")
    parser.add_argument("--asset-class", type=str, help="Asset class override")
    parser.add_argument("--description", type=str, help="Strategy description")
    parser.add_argument("--dry-run", action="store_true", help="Preview without writing files")
    parser.add_argument("--list-templates", action="store_true", help="List available templates")
    parser.add_argument("--list-strategies", action="store_true", help="List all existing strategies")
    parser.add_argument("--json", action="store_true", help="Output as JSON")

    args = parser.parse_args()

    # ── List templates ──
    if args.list_templates:
        templates = list_templates()
        if args.json:
            print(json.dumps([{
                "name": t.name,
                "description": t.description,
                "base_class": t.base_class,
                "asset_classes": t.asset_classes,
                "default_symbols": t.default_symbols,
            } for t in templates], indent=2))
        else:
            print("\nAvailable Strategy Templates:\n")
            for t in templates:
                print(f"  {t.name:10s}  {t.base_class:25s}  {t.description}")
            print()
        return

    # ── List strategies ──
    if args.list_strategies:
        strategies = list_all_strategies()
        if args.json:
            print(json.dumps(strategies, indent=2))
        else:
            print(f"\nExisting Strategies ({len(strategies)}):\n")
            for s in strategies:
                cfg_icon = "Y" if s["has_config"] else " "
                print(f"  [{cfg_icon}] {s['strategy_id']:30s}  {s['base_class']:25s}  {s['category']:8s}  {s['lines']:>4d} lines")
            print(f"\n  [Y] = has YAML config\n")
        return

    # ── Scaffold ──
    if not args.name:
        parser.error("--name is required for scaffolding")

    result = scaffold_strategy(
        name=args.name,
        template_type=args.type,
        symbols=args.symbols,
        asset_class=args.asset_class,
        description=args.description,
        dry_run=args.dry_run,
    )

    if args.json:
        print(json.dumps({
            "strategy_id": result.strategy_id,
            "class_name": result.class_name,
            "template_type": result.template_type,
            "strategy_file": result.strategy_file,
            "config_file": result.config_file,
        }, indent=2))
    else:
        action = "PREVIEW (dry run)" if args.dry_run else "CREATED"
        print(f"\n  Strategy {action}:")
        print(f"    ID:       {result.strategy_id}")
        print(f"    Class:    {result.class_name}")
        print(f"    Template: {result.template_type}")
        print(f"    Code:     {result.strategy_file}")
        print(f"    Config:   {result.config_file}")
        print()

        if args.dry_run:
            print("─── Generated Code ───")
            print(result.strategy_code)
            print("\n─── Generated Config ───")
            print(result.config_yaml)


if __name__ == "__main__":
    main()
