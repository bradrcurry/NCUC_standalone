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
from sklearn.metrics import classification_report, f1_score
from sklearn.model_selection import StratifiedKFold, train_test_split


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


@dataclass
class CrossValidationResult:
    """Output of stratified k-fold CV.

    Reports mean ± std across folds on the eligible (>=k-samples) classes.
    Train-only classes are still trained on within each fold but contribute
    no eval signal — recorded separately so callers can see what's
    underrepresented.
    """

    n_folds: int
    eligible_n: int
    train_only_classes: list[str]
    fold_accuracies: list[float]
    fold_weighted_f1: list[float]
    fold_macro_f1: list[float]
    mean_accuracy: float
    std_accuracy: float
    mean_weighted_f1: float
    std_weighted_f1: float
    mean_macro_f1: float
    std_macro_f1: float


def cross_validate_baseline(
    dataset: TrainingDataset,
    n_folds: int = 5,
    random_state: int = 13,
    max_features: int = 20_000,
) -> CrossValidationResult:
    """Stratified k-fold CV over the eligible subset.

    Eligible = classes with at least ``n_folds`` gold samples (so each
    fold gets at least one example). Rare classes (<n_folds samples) are
    placed in EVERY fold's training set but contribute no eval — their
    metric column shows zero support.

    A single 80/20 split at n=441 has high variance (~3-5 pts of accuracy
    drift between random seeds in our corpus). CV gives a more honest
    number for comparing against future fine-tuned models.
    """
    label_counts = Counter(dataset.labels)
    train_only_classes = [
        lab for lab, n in label_counts.items() if n < n_folds
    ]
    eligible_idx = [
        i for i, lab in enumerate(dataset.labels)
        if lab not in train_only_classes
    ]
    rare_idx = [
        i for i, lab in enumerate(dataset.labels)
        if lab in train_only_classes
    ]
    if not eligible_idx:
        raise ValueError(
            f"No classes have >={n_folds} samples; cannot run CV. "
            f"Use train_baseline() with stratified split instead."
        )

    eligible_labels = [dataset.labels[i] for i in eligible_idx]
    skf = StratifiedKFold(n_splits=n_folds, shuffle=True, random_state=random_state)

    accuracies: list[float] = []
    weighted_f1s: list[float] = []
    macro_f1s: list[float] = []

    for fold_train_idx, fold_val_idx in skf.split(eligible_idx, eligible_labels):
        # Translate fold indices back to dataset indices
        train_global = [eligible_idx[i] for i in fold_train_idx] + rare_idx
        val_global = [eligible_idx[i] for i in fold_val_idx]
        train_texts = [dataset.texts[i] for i in train_global]
        train_labels = [dataset.labels[i] for i in train_global]
        val_texts = [dataset.texts[i] for i in val_global]
        val_labels = [dataset.labels[i] for i in val_global]

        # Fresh vectorizer + model per fold — fit on TRAIN only
        vectorizer = TfidfVectorizer(
            max_features=max_features,
            ngram_range=(1, 2),
            min_df=2,
            max_df=0.95,
            lowercase=True,
            strip_accents="unicode",
        )
        X_train = vectorizer.fit_transform(train_texts)
        X_val = vectorizer.transform(val_texts)
        model = LogisticRegression(
            class_weight="balanced",
            max_iter=2000,
            solver="lbfgs",
            random_state=random_state,
        )
        model.fit(X_train, train_labels)
        y_pred = model.predict(X_val)

        accuracies.append(float((y_pred == val_labels).mean()))
        weighted_f1s.append(float(f1_score(val_labels, y_pred, average="weighted", zero_division=0)))
        macro_f1s.append(float(f1_score(val_labels, y_pred, average="macro", zero_division=0)))

    def _mean_std(xs: list[float]) -> tuple[float, float]:
        m = sum(xs) / len(xs)
        var = sum((x - m) ** 2 for x in xs) / len(xs)
        return m, var ** 0.5

    mean_acc, std_acc = _mean_std(accuracies)
    mean_w_f1, std_w_f1 = _mean_std(weighted_f1s)
    mean_m_f1, std_m_f1 = _mean_std(macro_f1s)

    return CrossValidationResult(
        n_folds=n_folds,
        eligible_n=len(eligible_idx),
        train_only_classes=sorted(train_only_classes),
        fold_accuracies=[round(x, 4) for x in accuracies],
        fold_weighted_f1=[round(x, 4) for x in weighted_f1s],
        fold_macro_f1=[round(x, 4) for x in macro_f1s],
        mean_accuracy=round(mean_acc, 4),
        std_accuracy=round(std_acc, 4),
        mean_weighted_f1=round(mean_w_f1, 4),
        std_weighted_f1=round(std_w_f1, 4),
        mean_macro_f1=round(mean_m_f1, 4),
        std_macro_f1=round(std_m_f1, 4),
    )
