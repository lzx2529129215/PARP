#include "myself_kswapd/error.h"

const char *reclaim_error_name(enum reclaim_error error)
{
    switch (error) {
    case RECLAIM_OK: return "OK";
    case RECLAIM_ERR_INVALID_ARGUMENT: return "INVALID_ARGUMENT";
    case RECLAIM_ERR_NO_MEMORY: return "NO_MEMORY";
    case RECLAIM_ERR_DOMAIN_NOT_FOUND: return "DOMAIN_NOT_FOUND";
    case RECLAIM_ERR_DOMAIN_ALREADY_EXISTS: return "DOMAIN_ALREADY_EXISTS";
    case RECLAIM_ERR_DOMAIN_NOT_EMPTY: return "DOMAIN_NOT_EMPTY";
    case RECLAIM_ERR_PAGE_NOT_FOUND: return "PAGE_NOT_FOUND";
    case RECLAIM_ERR_PAGE_ALREADY_EXISTS: return "PAGE_ALREADY_EXISTS";
    case RECLAIM_ERR_PAGE_STATE: return "PAGE_STATE";
    case RECLAIM_ERR_PAGE_TYPE: return "PAGE_TYPE";
    case RECLAIM_ERR_PARSE: return "PARSE";
    case RECLAIM_ERR_EXECUTOR: return "EXECUTOR";
    case RECLAIM_ERR_VALIDATION: return "VALIDATION";
    case RECLAIM_ERR_NOT_SUPPORTED: return "NOT_SUPPORTED";
    case RECLAIM_ERR_INTERNAL: return "INTERNAL";
    default: return "UNKNOWN";
    }
}
