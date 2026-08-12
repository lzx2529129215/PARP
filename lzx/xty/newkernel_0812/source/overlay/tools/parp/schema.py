#!/usr/bin/env python3
"""Versioned userspace schema; the kernel never parses JSON."""

import struct

SCHEMA_VERSION = 1
APP_PRIOR = struct.Struct("<IIHHIQQQ")
APP_BIND = struct.Struct("<I Q I I Q Q Q Q ?")


def pack_app_prior(app_id, use_score, rank, horizon_ms, ttl_ns,
                   model_version, timestamp_ns):
    return APP_PRIOR.pack(SCHEMA_VERSION, app_id, use_score, rank, horizon_ms,
                          ttl_ns, model_version, timestamp_ns)


def pack_app_bind(domain_id, app_id, generation, updated_ns, expires_ns,
                  epoch_id, model_version, active=True):
    return APP_BIND.pack(SCHEMA_VERSION, domain_id, app_id, generation,
                         updated_ns, expires_ns, epoch_id, model_version,
                         active)
