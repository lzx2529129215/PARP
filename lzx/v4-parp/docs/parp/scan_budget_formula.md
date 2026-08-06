# Scan-budget formula

MGLRU's native value is in base-page scan units. A large folio consumes `folio_nr_pages()` units.

Default Q15 multipliers are foreground 16384 (0.50), high 19661 (about 0.60), medium 26214 (about 0.80), and low 39321 (about 1.20). Score thresholds are 24576 and 12288. The configurable multiplier envelope is 16384..49152 (0.50..1.50).

`scaled = round(native * multiplier / 32768)` using wide arithmetic. If native is nonzero, the minimum is one scan unit but never increases a protected native value beyond native. Increases are capped at native + 4096 units with saturation. Elevated pressure blends the multiplier halfway toward 1.0; high pressure floors protection at 0.75; emergency pressure returns native.

Higher probability cannot increase proposed scan units, and foreground protection is at least as strong as background at the same score.
