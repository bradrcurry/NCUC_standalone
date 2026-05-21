"""TF-IDF + LogisticRegression baseline for document-type classification.

Lives in its own module so the training logic can be tested without going
through the CLI wrapper. The architecture is intentionally minimal:

  - TfidfVectorizer over the first ~2000 chars of doc text
  - LogisticRegression with class_weight='balanced'
  - Per-class precision/recall/F1 reported on a stratified val split

The point of this baseline is to set a number Stream D can compare any
later (DistilBERT, qwen-finetuned, …) model against. It is NOT meant to
be state-of-the-art. Given the imbalanced 441-row gold set, even a
moderately good baseline will be hard for a fancier model to beat
without more data — which is itself a useful signal.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from typing import Any

from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import classification_report
from sklearn.model_selection import train_test_split


MIN_SAMPLES_FOR_VAL_SPLIT = 5
DEFAULT_VAL_FRACTION = 0.20


@dataclass
class TrainingDataset:
    """Materialized training rows. Built outside this module to keep DB
    access out of the trainer's surface."""

    hd_ids: list[int]
    labels: list[str]
    texts: list[str]


@dataclass
class TrainingResult:
    """Output of a baseline training run."""

    classes: list[str]
    train_n: int
    val_n: int
    train_only_classes: list[str]
    val_classification_report: dict[str, Any]
    val_accuracy: float
    overall_train_accuracy: float
    vectorizer: TfidfVectorizer
    model: LogisticRegression


def _stratified_split(
    dataset: TrainingDataset,
    val_fraction: float = DEFAULT_VAL_FRACTION,
    random_state: int = 13,
) -> tuple[TrainingDataset, TrainingDataset, list[str]]:
    """Return (train, val, train_only_classes).

    Stratified train/val for classes with >= MIN_SAMPLES_FOR_VAL_SPLIT
    samples. Classes with fewer samples are placed entirely in train —
    holding out a single example would make the per-class val metric
    binary and uninformative.
    """
    label_counts = Counter(dataset.labels)
    train_only_classes = [
        lab for lab, n in label_counts.items() if n < MIN_SAMPLES_FOR_VAL_SPLIT
    ]

    # Partition rows
    train_idx: list[int] = []
    eligible_idx: list[int] = []
    for i, label in enumerate(dataset.labels):
        if label in train_only_classes:
            train_idx.append(i)
        else:
            eligible_idx.append(i)

    # Stratified split on the eligible subset
    eligible_labels = [dataset.labels[i] for i in eligible_idx]
    if eligible_idx and len(set(eligible_labels)) >= 2:
        train_part, val_part = train_test_split(
            eligible_idx,
            test_size=val_fraction,
            stratify=eligible_labels,
            random_state=random_state,
        )
        train_idx.extend(train_part)
        val_idx = val_part
    else:
        # All eligible classes have 0/1 instances → no meaningful split
        train_idx.extend(eligible_idx)
        val_idx = []

    def _subset(idxs: list[int]) -> TrainingDataset:
        return TrainingDataset(
            hd_ids=[dataset.hd_ids[i] for i in idxs],
            labels=[dataset.labels[i] for i in idxs],
            texts=[dataset.texts[i] for i in idxs],
        )

    return _subset(train_idx), _subset(val_idx), train_only_classes


def train_baseline(
    dataset: TrainingDataset,
    val_fraction: float = DEFAULT_VAL_FRACTION,
    random_state: int = 13,
    max_features: int = 20_000,
) -> TrainingResult:
    """Fit a TfidfVectorizer + LogisticRegression on ``dataset``.

    Returns a TrainingResult with the fitted artifacts and per-class
    val metrics. Callers can persist ``result.model`` and
    ``result.vectorizer`` via joblib if they want to reuse it.
    """
    train, val, train_only_classes = _stratified_split(
        dataset, val_fraction=val_fraction, random_state=random_state
    )

    # Vectorize — fit on TRAIN only so val metrics aren't optimistic
    vectorizer = TfidfVectorizer(
        max_features=max_features,
        ngram_range=(1, 2),
        min_df=2,
        max_df=0.95,
        lowercase=True,
        strip_accents="unicode",
    )
    X_train = vectorizer.fit_transform(train.texts)
    y_train = train.labels

    model = LogisticRegression(
        class_weight="balanced",
        max_iter=2000,
        # lbfgs handles multinomial logistic natively in sklearn >=1.0.
        # On small corpora (~440 rows) this converges in <1s.
        solver="lbfgs",
        random_state=random_state,
    )
    model.fit(X_train, y_train)

    # Train accuracy (reference; will be optimistic)
    overall_train_accuracy = float(model.score(X_train, y_train))

    if val.texts:
        X_val = vectorizer.transform(val.texts)
        y_val_pred = model.predict(X_val)
        val_classification_report = classification_report(
            val.labels,
            y_val_pred,
            output_dict=True,
            zero_division=0,
        )
        val_accuracy = float((y_val_pred == val.labels).mean()) if val.labels else 0.0
    else:
        val_classification_report = {}
        val_accuracy = 0.0

    return TrainingResult(
        classes=sorted(set(dataset.labels)),
        train_n=len(train.texts),
        val_n=len(val.texts),
        train_only_classes=sorted(train_only_classes),
        val_classification_report=val_classification_report,
        val_accuracy=val_accuracy,
        overall_train_accuracy=overall_train_accuracy,
        vectorizer=vectorizer,
        model=model,
    )
