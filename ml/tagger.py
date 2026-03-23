# ml/tagger.py
# Tag prediction for ToMenu — TF-IDF + multi-label LogisticRegression.
#
# Two modes:
#   1. Rule-based fallback  — always available, no training required
#      (mirrors the keyword rules in scrapers/namenu.scrape.py)
#   2. ML model             — trained on human-verified examples from the DB
#      Loaded on first call; falls back to rules silently if unavailable.
#
# Training reads exclusively from ml_training_examples in namenu.db:
#   • ml_training_examples  — human-verified tags saved via the Bulk Editor
#
# Auto-tagged rows from menu_items are intentionally excluded — the model should
# only learn from corrections you've confirmed, not from the rules it's meant to improve on.
#
# Usage:
#   from ml.tagger import predict, train, model_status
#
#   tags = predict("Kuracie na grile so smotanou", "main", "so zeleninou")
#   result = train("namenu.db")  # → dict with stats
#   status = model_status()      # → dict with metadata

import json
import os
import pickle
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Optional

# ── paths ─────────────────────────────────────────────────────────────────────

# Model files go in the data directory (env var) so they persist in Docker volumes.
# Falls back to the ml/ source directory for local development.
_data_dir = Path(os.environ.get("NAMENU_DB", "namenu.db")).parent
ML_DIR    = _data_dir / "ml"
MODEL_PATH = ML_DIR / "model.pkl"
META_PATH  = ML_DIR / "meta.json"

# ── rule-based fallback (mirrored from scrapers/namenu.scrape.py) ─────────────

MEAT_KEYWORDS = [
    "mäso", "mäsov", "rezen", "rezeň", "bravčov", "bravčové",
    "hovädz", "hovädzí", "hovädzieho", "jahňa", "teľac",
    "šunka", "slanina", "bôčik", "reberc", "biftek", "roastbeef",
    "mleté", "mletého", "sekaná", "pečené mäso",
    "kura", "kurací", "kuracích", "kurča", "kurčaťa",
    "morka", "morčac", "kačic", "králik",
]

FISH_KEYWORDS = [
    "ryba", "rybacia", "losos", "treska", "pstruh", "kapor",
    "filé", "tuniak", "sardina", "platesa", "tilapia",
]

TAG_RULES: dict[str, list[str]] = {
    "chicken":  ["kura", "kurací", "kuracích", "kurča", "kurčaťa"],
    "pork":     ["bravčov", "bravčové", "šunka", "slanina", "bôčik", "reberc"],
    "beef":     ["hovädz", "hovädzí", "hovädzieho", "biftek", "roastbeef"],
    "fish":     ["ryba", "rybacia", "losos", "treska", "pstruh", "kapor",
                 "filé", "tuniak", "sardina", "platesa", "tilapia"],
    "fried":    ["vyprážan", "smaž", "smažen", "fritovan"],
    "grilled":  ["grilovan", "na grile", "grill"],
    "baked":    ["pečen", "zapékan", "zapečen", "v rúre"],
    "steamed":  ["dusené", "dusen", "varené v pare"],
    "pasta":    ["halušky", "cestoviny", "špagety", "tagliatelle",
                 "penne", "fusilli", "lasagne", "rezance", "noky"],
    "rice":     ["ryža", "rizoto", "pilaf"],
    "salad":    ["šalát", "salát"],
    "soup":     ["polievka", "vývar", "gulášová", "kapustnica",
                 "šošovicová", "fazuľová", "paradajková", "zemiaková pol"],
    "burger":   ["burger", "hamburger"],
    "sandwich": ["sendvič", "toast", "bagel", "wrap"],
    "pizza":    ["pizza", "pizz"],
    "asian":    ["čínsk", "japonsk", "thajsk", "wok", "sushi",
                 "nudle", "ramen", "pho"],
    "dessert":  ["dezert", "lievance", "lievančeky", "buchty", "šišky",
                 "nákyp", "tiramisu", "palacinky", "koláč", "torta"],
    "dairy":    ["bryndza", "syr", "syrov", "smotana", "maslo",
                 "jogurt", "mozzarella", "parmezan", "ricotta"],
    "egg":      ["vajce", "vajíčko", "omeleta", "praženica"],
    "spicy":    ["pálivý", "pálivé", "chili", "jalapeño", "korenený"],
    "sweet":    ["sladký", "med", "karamel", "čokolád", "vanilk", "ovocn"],
    "healthy":  ["celozrnn", "nízkotučn", "light", "fit", "bezlepkov"],
    "vegan":    ["tofu", "hummus", "seitan", "tempeh"],
}

VEGETARIAN_ONLY_KEYWORDS = [
    "špenát", "zelenin", "zeleninov", "bryndza",
    "tofu", "hummus", "cícer", "šošovica", "fazuľa",
    "hrášok", "brokolica", "karfiol", "vegán", "vegetarián",
]


def auto_tag(dish_name: str, dish_type: str, description: str = "") -> list[str]:
    """Rule-based tagger — always available, no model required."""
    text = ((dish_name or "") + " " + (description or "")).lower()
    tags: set[str] = set()

    if dish_type == "soup":
        tags.add("soup")

    has_meat = any(kw in text for kw in MEAT_KEYWORDS)
    has_fish = any(kw in text for kw in FISH_KEYWORDS)

    if has_meat:
        tags.add("meat")
    if has_fish:
        tags.add("fish")

    for tag, keywords in TAG_RULES.items():
        for kw in keywords:
            if kw in text:
                tags.add(tag)
                break

    if not has_meat and not has_fish:
        for kw in VEGETARIAN_ONLY_KEYWORDS:
            if kw in text:
                tags.add("vegetarian")
                break

    return sorted(tags)


# ── model cache ───────────────────────────────────────────────────────────────
# Loaded once per process lifetime, reloaded when the file changes.

_model_cache: dict = {}


def _load_model() -> Optional[dict]:
    """
    Returns the loaded model bundle or None if no model exists.
    Caches by file mtime so a retrain is picked up without a restart.
    """
    global _model_cache
    if not MODEL_PATH.exists():
        return None
    mtime = MODEL_PATH.stat().st_mtime
    if _model_cache.get("mtime") == mtime:
        return _model_cache
    try:
        with open(MODEL_PATH, "rb") as f:
            bundle = pickle.load(f)
        bundle["mtime"] = mtime
        _model_cache = bundle
        return _model_cache
    except Exception:
        return None


# ── predict ───────────────────────────────────────────────────────────────────

# Minimum probability for the ML model to assert a tag
ML_THRESHOLD = 0.25

# Confidence below which we also include the rule-based result for that tag
HYBRID_FALLBACK_THRESHOLD = 0.40


def predict(
    dish_name: str,
    dish_type: str,
    description: str = "",
    *,
    return_scores: bool = False,
) -> list[str] | dict:
    """
    Predict tags for a dish using the ML model when available,
    falling back to rules for any tag where the model is uncertain.

    Args:
        dish_name:    Slovak dish name
        dish_type:    "main" | "soup" | "dessert"
        description:  Optional description text
        return_scores: If True, returns {"tags": [...], "scores": {...}, "source": "ml"|"rules"}
                       instead of just the tag list.

    Returns:
        Sorted list of tag strings (or dict if return_scores=True).
    """
    rule_tags = set(auto_tag(dish_name, dish_type, description))
    bundle    = _load_model()

    if bundle is None:
        # No model trained yet — pure rules
        result_tags = sorted(rule_tags)
        if return_scores:
            return {
                "tags":   result_tags,
                "scores": {t: 1.0 for t in result_tags},
                "source": "rules",
            }
        return result_tags

    try:
        vectorizer  = bundle["vectorizer"]
        classifier  = bundle["classifier"]
        label_names = bundle["label_names"]

        text    = _build_text(dish_name, dish_type, description)
        X       = vectorizer.transform([text])
        # Probabilities: shape (1, n_labels)
        proba   = classifier.predict_proba(X)[0]

        ml_tags: dict[str, float] = {}
        for label, prob in zip(label_names, proba):
            if prob >= ML_THRESHOLD:
                ml_tags[label] = round(float(prob), 3)

        # Hybrid: start with ML tags, fill in rule tags when ML is uncertain
        final_tags: dict[str, float] = dict(ml_tags)
        for tag in rule_tags:
            if tag not in final_tags:
                # Rules fired but ML missed — include if ML gave it at least some weight
                # OR if it's a structural tag (soup type)
                tag_idx = label_names.index(tag) if tag in label_names else -1
                rule_prob = float(proba[tag_idx]) if tag_idx >= 0 else 0.0
                if rule_prob >= HYBRID_FALLBACK_THRESHOLD or tag == dish_type:
                    final_tags[tag] = max(rule_prob, 0.5)
                elif tag in ("soup",) and dish_type == "soup":
                    final_tags[tag] = 1.0

        result_tags = sorted(final_tags.keys())

        if return_scores:
            return {
                "tags":   result_tags,
                "scores": final_tags,
                "source": "ml+rules",
            }
        return result_tags

    except Exception:
        # Any model error → fall back to pure rules silently
        result_tags = sorted(rule_tags)
        if return_scores:
            return {
                "tags":   result_tags,
                "scores": {t: 1.0 for t in result_tags},
                "source": "rules-fallback",
            }
        return result_tags


# ── train ─────────────────────────────────────────────────────────────────────

# Minimum number of human-verified examples for a tag to be included in the model.
# Tags below this threshold are left to the rule-based fallback.
MIN_TAG_EXAMPLES = 2


def train(db_path: str) -> dict:
    """
    Train the multi-label classifier exclusively on human-verified examples
    from ml_training_examples (saved via the Bulk Editor → save + train).

    Returns a dict with training statistics:
        {
          "ok": True,
          "examples": int,          # total training rows used
          "ml_examples": int,       # rows from ml_training_examples
          "labels": int,            # number of tag classes learned
          "tag_stats": [            # per-tag sample count
              {"tag": str, "count": int}, ...
          ],
          "trained_at": str,        # ISO datetime
          "model_path": str,
        }
    """
    try:
        from sklearn.feature_extraction.text import TfidfVectorizer
        from sklearn.linear_model import LogisticRegression
        from sklearn.multiclass import OneVsRestClassifier
        from sklearn.preprocessing import MultiLabelBinarizer
    except ImportError as e:
        return {"ok": False, "error": f"scikit-learn not installed: {e}"}

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    # ── 1. Collect training data (human-verified only) ───────────────────────
    texts:  list[str]       = []
    labels: list[list[str]] = []
    ml_count = 0

    try:
        ex_rows = conn.execute("""
            SELECT e.item_id, e.item_name, e.tags
            FROM ml_training_examples e
            WHERE e.item_name IS NOT NULL AND e.tags IS NOT NULL
        """).fetchall()

        for ex in ex_rows:
            try:
                tag_list = json.loads(ex["tags"])
            except Exception:
                continue
            if not tag_list:
                continue
            # Fetch type + description from the original menu item for richer features
            orig = conn.execute(
                "SELECT type, description FROM menu_items WHERE id = ? LIMIT 1",
                (ex["item_id"],)
            ).fetchone()
            item_type = orig["type"] if orig else "main"
            item_desc = (orig["description"] or "") if orig else ""
            text = _build_text(ex["item_name"], item_type, item_desc)
            texts.append(text)
            labels.append(tag_list)
            ml_count += 1

    except sqlite3.OperationalError:
        pass  # table doesn't exist yet

    conn.close()

    total = len(texts)
    if total < 5:
        return {
            "ok":    False,
            "error": f"Not enough human-verified examples ({total}). "
                     "Use the Bulk Editor to tag and save items with 'save + train' first.",
        }

    # ── 2. Filter tags with too few examples ──────────────────────────────────
    from collections import Counter
    tag_counts = Counter(t for tag_list in labels for t in tag_list)
    valid_tags = {tag for tag, cnt in tag_counts.items() if cnt >= MIN_TAG_EXAMPLES}

    # Re-filter label lists to only valid tags
    labels_filtered = [
        [t for t in tag_list if t in valid_tags]
        for tag_list in labels
    ]
    # Drop rows with no valid labels left
    keep = [(t, l) for t, l in zip(texts, labels_filtered) if l]
    if len(keep) < 10:
        return {
            "ok":    False,
            "error": f"After filtering low-count tags, only {len(keep)} usable examples remain. "
                     "Add more training examples.",
        }
    texts_clean, labels_clean = zip(*keep)

    # ── 3. Vectorize ──────────────────────────────────────────────────────────
    vectorizer = TfidfVectorizer(
        analyzer  = "char_wb",   # character n-grams — great for Slovak morphology
        ngram_range = (2, 4),
        min_df    = 2,
        max_features = 8000,
        sublinear_tf = True,
    )
    X = vectorizer.fit_transform(texts_clean)

    # ── 4. Binarize labels ────────────────────────────────────────────────────
    mlb = MultiLabelBinarizer()
    Y   = mlb.fit_transform(labels_clean)

    # ── 5. Train classifier ───────────────────────────────────────────────────
    base_clf = LogisticRegression(
        max_iter  = 1000,
        C         = 1.5,
        solver    = "lbfgs",
        class_weight = "balanced",
    )
    clf = OneVsRestClassifier(base_clf, n_jobs=-1)
    clf.fit(X, Y)

    # ── 6. Save model ─────────────────────────────────────────────────────────
    ML_DIR.mkdir(parents=True, exist_ok=True)

    bundle = {
        "vectorizer":  vectorizer,
        "classifier":  clf,
        "label_names": list(mlb.classes_),
        "mlb":         mlb,
    }
    with open(MODEL_PATH, "wb") as f:
        pickle.dump(bundle, f)

    # Invalidate cache so next predict() call reloads
    global _model_cache
    _model_cache = {}

    # ── 7. Build stats ────────────────────────────────────────────────────────
    trained_at = datetime.now().isoformat()
    tag_stats  = sorted(
        [{"tag": tag, "count": tag_counts[tag]} for tag in valid_tags],
        key=lambda x: -x["count"],
    )

    meta = {
        "trained_at":   trained_at,
        "examples":     len(keep),
        "ml_examples":  ml_count,
        "labels":       len(mlb.classes_),
        "label_names":  list(mlb.classes_),
        "tag_stats":    tag_stats,
        "model_path":   str(MODEL_PATH),
    }
    with open(META_PATH, "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)

    return {"ok": True, **meta}


# ── model_status ──────────────────────────────────────────────────────────────

def model_status() -> dict:
    """
    Returns metadata about the current model, or a 'not trained' status.
    Does not load the model weights — reads from meta.json only.
    """
    if not META_PATH.exists():
        return {
            "trained": False,
            "message": "No model trained yet. Use POST /admin/ml/train to train.",
        }
    try:
        with open(META_PATH, "r", encoding="utf-8") as f:
            meta = json.load(f)
        meta["trained"] = True
        meta["model_exists"] = MODEL_PATH.exists()
        return meta
    except Exception as e:
        return {"trained": False, "message": f"Error reading metadata: {e}"}


# ── helpers ───────────────────────────────────────────────────────────────────

def _build_text(name: str, dish_type: str, description: str) -> str:
    """
    Combine dish fields into a single string for vectorization.
    Prefix the type so the model can learn type-specific patterns.
    """
    parts = [f"__type_{dish_type}__"]
    if name:
        parts.append(name.lower())
    if description:
        parts.append(description.lower())
    return " ".join(parts)