# SPDX-License-Identifier: GPL-2.0
obj-y += parp.o
parp-y := core/engine.o core/domain.o core/snapshot.o core/policy.o \
	  core/watermark.o \
	  core/budget.o core/scan_budget.o core/fallback.o core/stats.o core/evidence.o \
	  model/model_store.o model/app_prior.o model/state_assigner.o \
	  model/state_predictor.o model/file_predictor.o model/anon_predictor.o \
	  mapping/file_region.o mapping/anon_region.o mapping/lifecycle.o \
	  mapping/region_alignment.o \
	  adapter/memcg_adapter.o adapter/mglru_adapter.o \
	  adapter/file_adapter.o adapter/anon_adapter.o adapter/feedback_adapter.o \
	  adapter/scan_budget_adapter.o \
	  control/debugfs.o control/sysfs.o control/genl.o control/trace.o
parp-$(CONFIG_PARP_DAMON_ALIGNMENT) += adapter/damon_adapter.o
parp-$(CONFIG_PARP_FRONTIER_SCORE) += core/frontier_score.o
parp-$(CONFIG_PARP_EFFECTIVE_TIER) += core/effective_tier.o
obj-$(CONFIG_PARP_KUNIT_TEST) += tests/parp_test.o
