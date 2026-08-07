# Data model

File identity is device, inode, version, and page-index range. Anonymous
identity is domain, foreground epoch, mm cookie, role, VMA signature, and
relative range. Both are bounded side-table keys, never page fields.
