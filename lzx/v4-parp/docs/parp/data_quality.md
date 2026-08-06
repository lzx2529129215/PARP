# Data quality

Quality gates cover target→mm, mm→observation-domain, domain→AppBind, VMA
alignment, classified-byte coverage, ambiguity, staleness, persistence safety,
anonymous session confidence, duplicates, disorder, churn, and table drops.
Observation owner is the DAMON target task/mm and its current memcg/AppBind.
Page charge owner is the folio's actual memcg and is rechecked during file
lookup; the two are never assumed identical.

