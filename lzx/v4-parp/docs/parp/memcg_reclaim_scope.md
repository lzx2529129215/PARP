# Memcg reclaim scope

The Linux adapter maps the actual 6.17.13 context into a neutral scope using `target_mem_cgroup`, kswapd state and the proactive bit. Null/root targets become GLOBAL_KSWAPD or GLOBAL_DIRECT; non-root proactive targets become PROACTIVE_MEMCG; non-root non-proactive targets become TARGET_MEMCG.

Only TARGET_MEMCG is Apply-eligible. Global paths never use an application multiplier. Proactive reclaim remains native by default. At the chosen hook Linux 6.17.13 does not retain enough origin data to distinguish memory.high from other non-proactive target reclaim, so MEMCG_HIGH is defined but not emitted.
