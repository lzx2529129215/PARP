# File region mapping

For a VMA segment, `file_page_start = vm_pgoff + ((start-vm_start) >>
PAGE_SHIFT)`; all operations are checked for alignment and overflow. Keys use
device, inode, qualified file version, logical start, and page count. i_version
has highest confidence, inode generation is next, and a size/inode session hash
is weak. SHMEM, TMPFS, deleted, special, and weak-version mappings are never
claimed persistence-safe. Paths are neither required nor exported.

