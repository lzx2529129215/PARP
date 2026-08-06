# Anonymous region mapping

Anonymous identity is session-only:
domain + foreground epoch + mm cookie + process role + semantic VMA signature
+ relative range. The signature hashes a whitelist of VMA semantics, length
bin, class, role, and optional controlled name hash; it never contains a kernel
pointer. Epoch/mm/VMA/bind/TTL changes invalidate identity. App prior can
modulate evidence but cannot independently label anonymous memory cold.

