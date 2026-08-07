# Kernel integration

The only native hooks are Kconfig, Makefile, and `scan_folios()` in
`mm/vmscan.c`. Prepare occurs once per scan batch, score/apply once per
candidate folio, and finish after accounting. Apply is inert in this milestone.
