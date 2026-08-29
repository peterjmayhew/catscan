# Reference photos (for the cloud AI backend)

Drop 3-6 clear, well-lit `.jpg`/`.jpeg` photos of your own cat(s) here to
enable `DETECTION_BACKEND=cloud` in `server/detector.py` (see the main
README's "Reliable identification" section).

Unlike `data/my_cat/` for training the local model, this doesn't need to be
a big dataset - a handful of representative photos (different angles, at
least one showing the full body and coat clearly) is enough, since these
are sent as reference images to Claude's vision model on every request
rather than used to train anything.

If you have multiple cats, include a couple of photos of each so the model
learns to say "yes" to any of them, not just one.
