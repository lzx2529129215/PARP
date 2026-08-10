
# 水位线相关逻辑：
`zone_watermark_ok`:
读取该 zone 的 `NR_FREE_PAGES`，再结合本次申请的
- 需要几页连续内存（`order`）
- GFP 分配标志
- 目标 zone
- 预留页等限制
判断是否还能安全分配
```
bool zone_watermark_ok(struct zone *z, unsigned int order, unsigned long mark,int highest_zoneidx, unsigned int alloc_flags)
{

    return __zone_watermark_ok(z, order, mark, highest_zoneidx, alloc_flags,

                    zone_page_state(z, NR_FREE_PAGES));

}
```

分配主路径为：
```
alloc_pages()
	└─__alloc_pages_noprof()
		  └─ get_page_from_freelist()
		       └─ zone_watermark_fast()
		            └─ __zone_watermark_ok()
```


可以把 `kswapd` 理解为**事件驱动的后台线程**：它平时睡眠，因分配器发现水位不足而被唤醒；不是每隔固定时间轮询整个系统内存

**`__alloc_pages_noprof`负责主要的页面分配逻辑: **
```
__alloc_pages_noprof()
  └─ __alloc_frozen_pages_noprof()
       └─ get_page_from_freelist()
            ├─ zone_watermark_fast()  // 水位线检查
            ├─ node_reclaim()         // 可选的本地 node 回收
            ├─ rmqueue()              // 从空闲链表/PCP 中取页
            └─ prep_new_page()        // 初始化分配得到的页
```

在每次页面分配的时候，都会进行一次水位线的检查
- 具体来说，`get_page_from_freelist()` 会遍历本次请求可用的 zone，并对每个 zone 调用：````
zone_watermark_fast(zone, order, mark, ...)
- 对于快速检查函数`zone_watermark_fast()`而言，只被普通 `alloc_pages()` / `alloc_page()`和 批量分配 `alloc_pages_bulk()` 调用 
- 