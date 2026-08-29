# Training data

`train_classifier.py` expects three folders here, matching the labels in
`server/detector.py`:

```
data/
  my_cat/      - photos of your own cat(s)
  other_cat/   - photos of the neighbour's cat (and any other cats you want
                 flagged as "not mine")
  no_cat/      - photos with no cat at all (empty patio, other animals,
                 leaves blowing past the PIR sensor, etc.) - important so
                 the model learns what "nothing to see here" looks like
```

See also `data/reference_photos/README.md` if you're using the cloud AI
backend instead of (or alongside) a trained model - it needs far fewer
photos and no training step.

## Where to get the photos

The easiest way is to let the server run in heuristic mode for a week or
two - every frame it receives is auto-saved under `captures/<label>/` in
the repo root. Review those, move the correctly-labelled ones into the
matching `data/` folder (and re-file any the heuristic got wrong), and
you'll have a real-world dataset for free.

## Tips for a better model

- Aim for at least ~100 images per class; several hundred is better.
- Include different times of day, lighting, and the cat at different
  distances/angles - that's the variation the camera will actually see.
- If you have more than one cat, include photos of all of them in
  `my_cat/` so the model learns "these specific cats", not just one of them.
- If your neighbour's cat is also a tabby, make sure `other_cat/` includes
  plenty of examples of it specifically - the model needs to see that a
  tabby coat alone doesn't mean "my cat" to actually learn the distinction.
- Keep `no_cat/` photos genuinely representative of false triggers (wind,
  shadows, other animals) rather than just empty daylight shots.
