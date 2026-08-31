# Scenario Judgment and Prediction Accuracy Report

- Scenario count: 6
- Judgment accuracy: 16.67%
- Rule trend prediction accuracy: 0.0%
- Second-order Markov prediction accuracy: 0.0%

## Per-scenario result

### stable_hot
- expected dominant: STABLE_HOT
- actual dominant: MULTI_HOTSPOT
- judgment correct: False
- expected next (rule_trend): STABLE_HOT
- actual next (rule_trend): MULTI_HOTSPOT
- rule_trend prediction correct: False
- expected next (second_order_markov): STABLE_HOT
- actual next (second_order_markov): MULTI_HOTSPOT
- second_order_markov prediction correct: False

### burst_expansion
- expected dominant: BURST_EXPANSION
- actual dominant: BURST_EXPANSION
- judgment correct: True
- expected next (rule_trend): STABLE_HOT
- actual next (rule_trend): STABLE_HOT
- rule_trend prediction correct: False
- expected next (second_order_markov): BURST_EXPANSION
- actual next (second_order_markov): BURST_EXPANSION
- second_order_markov prediction correct: False

### streaming
- expected dominant: STREAMING
- actual dominant: MULTI_HOTSPOT
- judgment correct: False
- expected next (rule_trend): STREAMING
- actual next (rule_trend): MULTI_HOTSPOT
- rule_trend prediction correct: False
- expected next (second_order_markov): STREAMING
- actual next (second_order_markov): MULTI_HOTSPOT
- second_order_markov prediction correct: False

### random_unknown
- expected dominant: UNKNOWN
- actual dominant: MULTI_HOTSPOT
- judgment correct: False
- expected next (rule_trend): UNKNOWN
- actual next (rule_trend): MULTI_HOTSPOT
- rule_trend prediction correct: False
- expected next (second_order_markov): UNKNOWN
- actual next (second_order_markov): MULTI_HOTSPOT
- second_order_markov prediction correct: False

### cold
- expected dominant: LOW_VALUE_COLD
- actual dominant: UNKNOWN
- judgment correct: False
- expected next (rule_trend): LOW_VALUE_COLD
- actual next (rule_trend): UNKNOWN
- rule_trend prediction correct: False
- expected next (second_order_markov): LOW_VALUE_COLD
- actual next (second_order_markov): UNKNOWN
- second_order_markov prediction correct: False

### multi_hotspot
- expected dominant: MULTI_HOTSPOT
- actual dominant: BURST_EXPANSION
- judgment correct: False
- expected next (rule_trend): MULTI_HOTSPOT
- actual next (rule_trend): STABLE_HOT
- rule_trend prediction correct: False
- expected next (second_order_markov): MULTI_HOTSPOT
- actual next (second_order_markov): BURST_EXPANSION
- second_order_markov prediction correct: False
