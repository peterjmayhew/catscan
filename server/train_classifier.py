"""Fine-tunes a MobileNetV2 classifier on your own cat photos.

Expects images sorted into:
    data/my_tabby/*.jpg
    data/other_cat/*.jpg
    data/no_cat/*.jpg

(matching detector.LABELS). Aim for at least ~100 images per class - more
is better, and include a variety of lighting/angles/times of day, since
that's exactly the variation the camera will see in the wild.

Usage:
    python3 train_classifier.py [--epochs 15] [--data-dir ../data]

Produces:
    models/cat_classifier.h5       - Keras model, used directly by detector.py
    models/cat_classifier.tflite   - Quantized version, for future on-device use
"""

import argparse
import os

import tensorflow as tf

from detector import LABELS

IMAGE_SIZE = (224, 224)
BATCH_SIZE = 16


def build_datasets(data_dir):
    train_ds = tf.keras.utils.image_dataset_from_directory(
        data_dir,
        validation_split=0.2,
        subset="training",
        seed=42,
        image_size=IMAGE_SIZE,
        batch_size=BATCH_SIZE,
        class_names=LABELS,
    )
    val_ds = tf.keras.utils.image_dataset_from_directory(
        data_dir,
        validation_split=0.2,
        subset="validation",
        seed=42,
        image_size=IMAGE_SIZE,
        batch_size=BATCH_SIZE,
        class_names=LABELS,
    )
    autotune = tf.data.AUTOTUNE
    return train_ds.prefetch(autotune), val_ds.prefetch(autotune)


def build_model():
    augmentation = tf.keras.Sequential(
        [
            tf.keras.layers.RandomFlip("horizontal"),
            tf.keras.layers.RandomRotation(0.1),
            tf.keras.layers.RandomBrightness(0.2),
            tf.keras.layers.RandomZoom(0.1),
        ]
    )

    base_model = tf.keras.applications.MobileNetV2(
        input_shape=IMAGE_SIZE + (3,), include_top=False, weights="imagenet"
    )
    base_model.trainable = False

    inputs = tf.keras.Input(shape=IMAGE_SIZE + (3,))
    x = augmentation(inputs)
    x = tf.keras.applications.mobilenet_v2.preprocess_input(x * 255.0)
    x = base_model(x, training=False)
    x = tf.keras.layers.GlobalAveragePooling2D()(x)
    x = tf.keras.layers.Dropout(0.3)(x)
    outputs = tf.keras.layers.Dense(len(LABELS), activation="softmax")(x)
    model = tf.keras.Model(inputs, outputs)

    model.compile(
        optimizer=tf.keras.optimizers.Adam(1e-3),
        loss="sparse_categorical_crossentropy",
        metrics=["accuracy"],
    )
    return model


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--epochs", type=int, default=15)
    parser.add_argument(
        "--data-dir",
        default=os.path.join(os.path.dirname(__file__), "..", "data"),
    )
    args = parser.parse_args()

    train_ds, val_ds = build_datasets(args.data_dir)
    model = build_model()
    model.fit(train_ds, validation_data=val_ds, epochs=args.epochs)

    model_dir = os.path.join(os.path.dirname(__file__), "models")
    os.makedirs(model_dir, exist_ok=True)

    h5_path = os.path.join(model_dir, "cat_classifier.h5")
    model.save(h5_path)
    print(f"Saved {h5_path}")

    converter = tf.lite.TFLiteConverter.from_keras_model(model)
    converter.optimizations = [tf.lite.Optimize.DEFAULT]
    tflite_model = converter.convert()
    tflite_path = os.path.join(model_dir, "cat_classifier.tflite")
    with open(tflite_path, "wb") as f:
        f.write(tflite_model)
    print(f"Saved {tflite_path}")


if __name__ == "__main__":
    main()
