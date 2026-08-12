# PARP frontier score reference

This directory defines the integer contract shared by offline replay and the
kernel frontier scorer.

The bundled WPS, QQ, FILES and Generic tables are deterministic engineering
models for implementation and consistency testing. They were not trained on
the old Phase2.10 V1/V2 candidate concatenations and must not be represented as
production-quality reuse models. Promotion remains disabled until a genuine
post-`sort_folio()` frontier dataset is collected and passes the SHADOW gate.

The eight schema-v1 features, in fixed order, are:

1. access age in milliseconds;
2. previous access interval in milliseconds;
3. access EMA in Q8;
4. reuse interval EMA in milliseconds;
5. consecutive inactive scan count;
6. distance from the youngest MGLRU generation;
7. time in the current generation in milliseconds;
8. App-level LSTM reentry score in Q15.

An edge belongs to the lower bin. A missing feature is represented by
`INT64_MIN` and forces Native fallback. The score is an integer reuse score,
not a calibrated probability.

Run the reference tests from the kernel tree root:

```sh
python3 -m unittest -v tools.parp.frontier_score.tests.test_reference
python3 tools/parp/frontier_score/benchmark.py --iterations 20000
```

Run the kernel suite without installing or booting this kernel:

```sh
tools/testing/kunit/kunit.py run --arch=um \
  --kunitconfig tools/parp/frontier_score/.kunitconfig parp
```
