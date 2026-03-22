"""Sentiment features from news and social media.

Provides NLP-based sentiment scoring for event-driven strategies.
"""

from __future__ import annotations

from typing import Dict, List, Optional

import pandas as pd

from core.logging import get_logger

logger = get_logger(__name__)


def compute_news_sentiment_ma(df: pd.DataFrame, period: int = 7) -> pd.Series:
    """Moving average of news sentiment scores."""
    if "sentiment_score" not in df.columns:
        return pd.Series(0.0, index=df.index)
    return df["sentiment_score"].rolling(window=period).mean()


def compute_sentiment_momentum(df: pd.DataFrame, period: int = 5) -> pd.Series:
    """Change in sentiment over period."""
    if "sentiment_score" not in df.columns:
        return pd.Series(0.0, index=df.index)
    return df["sentiment_score"].diff(period)


def compute_news_volume(df: pd.DataFrame, period: int = 7) -> pd.Series:
    """Rolling count of news articles (if news_count column exists)."""
    if "news_count" not in df.columns:
        return pd.Series(0.0, index=df.index)
    return df["news_count"].rolling(window=period).sum()


SENTIMENT_FEATURES: Dict[str, tuple] = {
    "sentiment_ma_7": (lambda df: compute_news_sentiment_ma(df, 7), "sentiment", 7),
    "sentiment_momentum_5": (lambda df: compute_sentiment_momentum(df, 5), "sentiment", 5),
    "news_volume_7": (lambda df: compute_news_volume(df, 7), "sentiment", 7),
}


def register_sentiment_features(store) -> None:
    """Register sentiment features with a FeatureStore instance."""
    for name, (func, category, lookback) in SENTIMENT_FEATURES.items():
        store.register(
            name=name,
            func=func,
            category="sentiment",
            lookback=lookback,
        )
