# docker-already-exists-benign

A benign Docker layer line ('<hex> Already exists 0B') in the failed step must NOT drive the verdict (Step 9b); the real cause is the test failure in the pipeline logs.
