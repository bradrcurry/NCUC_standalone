"""Tests for the document-type baseline classifier (Stream D scaffold).

Locks the stratified-split-with-rare-class-fallback rule and confirms
the trainer returns the expected metric structure. Uses synthetic text
so the test runs in milliseconds — the real CLI integration test (with
Docling-text materialization) is left to manual verification.
"""
from __future__ import annotations

import pytest
from duke_rates.classification.baseline_classifier import (
    TrainingDataset,
    train_baseline,
    _stratified_split,
    MIN_SAMPLES_FOR_VAL_SPLIT,
)


def _make_dataset() -> TrainingDataset:
    """Build a synthetic dataset that mimics the real corpus shape:
    a few majority classes with many samples, several rare classes
    with 1-3 samples. Texts use distinct vocabularies per class so a
    TF-IDF + LR baseline can learn the boundary easily."""
    hd_ids: list[int] = []
    labels: list[str] = []
    texts: list[str] = []
    next_id = 1

    # Majority classes - 20 samples each, distinguishable vocabularies
    class_corpus = {
        "TARIFF_SHEET": [
            "leaf number five hundred residential service basic customer charge per kwh",
            "schedule res revised leaf availability monthly rate kilowatt",
        ],
        "ORDER_FINAL": [
            "before the north carolina utilities commission docket order approving",
            "it is therefore ordered commissioner final ruling commission",
        ],
        "TESTIMONY": [
            "direct testimony question please state your name background",
            "redirect examination witness sponsor exhibit qualifications",
        ],
    }
    for label, samples in class_corpus.items():
        for i in range(20):
            hd_ids.append(next_id); next_id += 1
            labels.append(label)
            texts.append(samples[i % len(samples)] + f" sample {i}")

    # Rare classes - 1-3 samples each
    rare_corpus = {
        "CERTIFICATE_OF_SERVICE": [
            "certificate of service i hereby certify served electronic filing",
            "certify served foregoing parties record copy via mail",
        ],
        "RIDER": [
            "rider ba annual billing adjustment leaf",
        ],
        "COVER_LETTER": [
            "via electronic filing enclosed please find sincerely",
            "re docket transmittal letter attached document",
        ],
    }
    for label, samples in rare_corpus.items():
        for s in samples:
            hd_ids.append(next_id); next_id += 1
            labels.append(label)
            texts.append(s)

    return TrainingDataset(hd_ids=hd_ids, labels=labels, texts=texts)


def test_stratified_split_pins_rare_classes_to_train():
    """Classes with <MIN_SAMPLES_FOR_VAL_SPLIT samples must end up entirely
    in train. The val set should only contain majority-class labels."""
    dataset = _make_dataset()
    train, val, train_only = _stratified_split(dataset, val_fraction=0.2, random_state=13)

    # CERTIFICATE_OF_SERVICE (2), RIDER (1), COVER_LETTER (2) are all rare
    assert set(train_only) == {"CERTIFICATE_OF_SERVICE", "RIDER", "COVER_LETTER"}
    # None of those should appear in val
    assert not (set(val.labels) & set(train_only))
    # val should still contain ~20% of majority-class samples (60 majority * 0.2 = 12)
    assert 8 <= len(val.labels) <= 16


def test_trainer_returns_per_class_metrics():
    """The training result must include classification_report with per-class
    precision/recall/F1 for the validation classes."""
    dataset = _make_dataset()
    result = train_baseline(dataset, val_fraction=0.2, random_state=13)

    assert result.train_n + result.val_n == len(dataset.labels)
    # Train accuracy should be near-perfect on this synthetic corpus
    assert result.overall_train_accuracy >= 0.9
    # Val accuracy should also be high given the distinct vocabularies
    assert result.val_accuracy >= 0.7
    # Per-class metrics present for the majority classes
    report = result.val_classification_report
    for label in ("TARIFF_SHEET", "ORDER_FINAL", "TESTIMONY"):
        assert label in report
        assert "precision" in report[label]
        assert "recall" in report[label]
        assert "f1-score" in report[label]


def test_trainer_handles_no_eligible_val_classes():
    """If no class meets the MIN_SAMPLES_FOR_VAL_SPLIT threshold, all rows
    go to train and val is empty. The trainer should not crash."""
    dataset = TrainingDataset(
        hd_ids=[1, 2, 3],
        labels=["A", "B", "A"],
        texts=["foo bar", "baz qux", "foo bar baz"],
    )
    result = train_baseline(dataset, val_fraction=0.5)
    assert result.val_n == 0
    assert result.train_n == 3
    assert result.val_accuracy == 0.0


def test_trainer_persists_fitted_artifacts():
    """The result must expose the fitted vectorizer + model so callers can
    serialize them (joblib.dump). Verify they're usable on a fresh sample."""
    dataset = _make_dataset()
    result = train_baseline(dataset, val_fraction=0.2, random_state=13)

    fresh_text = "leaf number five hundred basic customer charge per kwh"
    X = result.vectorizer.transform([fresh_text])
    pred = result.model.predict(X)[0]
    assert pred == "TARIFF_SHEET"


def test_classes_field_is_sorted_and_complete():
    dataset = _make_dataset()
    result = train_baseline(dataset, val_fraction=0.2)
    assert result.classes == sorted(set(dataset.labels))
    assert "TARIFF_SHEET" in result.classes
    assert "RIDER" in result.classes
