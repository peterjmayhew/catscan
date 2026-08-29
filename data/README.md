# Training data

`train_classifier.py` expects three folders here, matching the labels in
`server/detector.py`:

```
data/
  my_tabby/    - photos of your tabby cat(s)
  other_cat/   - photos of the neighbour's cat (and any other cats you want
                 flagged as "not mine")
  no_cat/      - photos with no cat at all (empty patio, other animals,
                 leaves blowing past the PIR sensor, etc.) - important so
                 the model learns what "nothing to see here" looks like
```

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
- If you have more than one tabby, include photos of all of them in
  `my_tabby/` so the model learns "tabby coat pattern", not "this one cat".
- Keep `no_cat/` photos genuinely representative of false triggers (wind,
  shadows, other animals) rather than just empty daylight shots.
