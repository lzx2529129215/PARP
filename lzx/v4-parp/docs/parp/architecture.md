# Architecture

`mm/parp/core`, `model`, and `mapping` use only PARP-owned value types.
`adapter` is the sole Linux-object boundary. Control-plane updates allocate a
new immutable snapshot and publish it with RCU. The MGLRU hook performs only a
bounded lookup and score operation.
