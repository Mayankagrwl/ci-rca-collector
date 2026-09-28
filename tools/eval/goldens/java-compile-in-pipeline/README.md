# java-compile-in-pipeline

The Step 9 regression: the failed step's own log is uninformative (setup/warning/pull-progress + exit); the real compile error lives only in the pipeline logs. The cause must come from the pipeline, not the benign job output.
