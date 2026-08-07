# Engine boundary

Core and model code cannot name `folio`, `page`, `lruvec`, `scan_control`,
`mem_cgroup`, `mm_struct`, `vm_area_struct`, or `damon_region`. A static check
enforces the boundary.
