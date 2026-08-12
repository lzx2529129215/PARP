/* SPDX-License-Identifier: GPL-2.0 */
#undef TRACE_SYSTEM
#define TRACE_SYSTEM parp

#if !defined(_TRACE_PARP_H) || defined(TRACE_HEADER_MULTI_READ)
#define _TRACE_PARP_H

#include <linux/tracepoint.h>

TRACE_EVENT(parp_decision,
	TP_PROTO(u64 domain_id, u8 mode, u8 page_type, u8 original,
		 u8 proposed, u8 applied, u8 fallback, u16 score),
	TP_ARGS(domain_id, mode, page_type, original, proposed, applied,
		fallback, score),
	TP_STRUCT__entry(
		__field(u64, domain_id)
		__field(u8, mode)
		__field(u8, page_type)
		__field(u8, original)
		__field(u8, proposed)
		__field(u8, applied)
		__field(u8, fallback)
		__field(u16, score)
	),
	TP_fast_assign(
		__entry->domain_id = domain_id;
		__entry->mode = mode;
		__entry->page_type = page_type;
		__entry->original = original;
		__entry->proposed = proposed;
		__entry->applied = applied;
		__entry->fallback = fallback;
		__entry->score = score;
	),
	TP_printk("domain=%llu mode=%u type=%u original=%u proposed=%u applied=%u fallback=%u score_q15=%u",
		  __entry->domain_id, __entry->mode, __entry->page_type,
		  __entry->original, __entry->proposed, __entry->applied,
		  __entry->fallback, __entry->score)
);

#endif
#include <trace/define_trace.h>
