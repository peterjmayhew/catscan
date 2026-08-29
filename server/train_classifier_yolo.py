"""Trains a YOLOv8 classification model on your own cat photos - a local,
TensorFlow-free alternative to train_classifier.py's MobileNetV2 pipeline.
Both do the same job (my_cat vs other_cat vs no_cat); pick whichever you'd
rather run (DETECTION_BACKEND=model uses the TensorFlow one,
DETECTION_BACKEND=yolo uses this one). Runs entirely on your PC's CPU or
GPU - no cloud calls.

Requires `pip install ultralytics` (already in requirements.txt).

Expects the same data/my_cat, data/other_cat, data/no_cat folders as
train_classifier.py (see data/README.md).

Usage:
    python3 train_classifier_yolo.py [--epochs 30] [--data-dir ../data]

Produces:
    models/cat_classifier_yolo.pt
"""

import argparse
import random
import shutil
from pathlib import Path

from ultralytics import YOLO

from detector import LABELS

VAL_FRACTION = 0.2
SEED = 42


def _prepare_split_dataset(source_dir: Path, staging_dir: Path):
    """YOLOv8's classification trainer expects <root>/train/<class>/*.jpg
    and <root>/val/<class>/*.jpg. This builds that layout (via symlinks, so
    it doesn't duplicate your image files) from the flat data/<class>/
    folders the rest of this project uses."""
    if staging_dir.exists():
        shutil.rmtree(staging_dir)

    rng = random.Random(SEED)
    for label in LABELS:
        class_dir = source_dir / label
        images = sorted(class_dir.glob("*.jpg")) + sorted(class_dir.glob("*.jpeg"))
        if not images:
            raise RuntimeError(f"No images found in {class_dir} - see data/README.md")

        rng.shuffle(images)
        split_index = max(1, int(len(images) * (1 - VAL_FRACTION)))
        splits = {"train": images[:split_index], "val": images[split_index:] or images[:1]}

        for split_name, split_images in splits.items():
            dest_dir = staging_dir / split_name / label
            dest_dir.mkdir(parents=True, exist_ok=True)
            for image_path in split_images:
                dest_path = dest_dir / image_path.name
                try:
                    dest_path.symlink_to(image_path.resolve())
                except OSError:
                    # Some filesystems (e.g. Windows without the right
                    # privileges) can't symlink - fall back to copying.
                    shutil.copy2(image_path, dest_path)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--epochs", type=int, default=30)
    parser.add_argument(
        "--data-dir", default=str(Path(__file__).parent / ".." / "data")
    )
    parser.add_argument(
        "--base-model",
        default="yolov8n-cls.pt",
        help="Ultralytics checkpoint to fine-tune; downloaded automatically on first use.",
    )
    args = parser.parse_args()

    source_dir = Path(args.data_dir).resolve()
    staging_dir = Path(__file__).parent / "yolo_dataset"
    _prepare_split_dataset(source_dir, staging_dir)

    project_dir = Path(__file__).parent / "runs"
    run_name = "cat_classifier"

    model = YOLO(args.base_model)
    model.train(
        data=str(staging_dir),
        epochs=args.epochs,
        imgsz=224,
        project=str(project_dir),
        name=run_name,
        exist_ok=True,  # overwrite the same run dir each time, for a predictable output path
    )

    # Ultralytics nests an extra task-name folder under project/name (e.g.
    # project/classify/name/weights/best.pt) - the exact layout has varied
    # across versions, so ask the trainer for the real path rather than
    # reconstructing it ourselves.
    best_weights = Path(model.trainer.best)
    if not best_weights.exists():
        raise RuntimeError(f"Training finished but expected weights not found at {best_weights}")

    model_dir = Path(__file__).parent / "models"
    model_dir.mkdir(exist_ok=True)
    dest = model_dir / "cat_classifier_yolo.pt"
    shutil.copy(best_weights, dest)
    print(f"Saved {dest}")


if __name__ == "__main__":
    main()
