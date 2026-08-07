/*
 * Test4B synthetic mixed working-set ballast.
 *
 * This process deliberately has no X11/Wayland dependency and never creates
 * a window.  It only allocates after a controller has sent ENTER_FOREGROUND
 * followed by ALLOCATE.  Once in BACKGROUND_IDLE it may touch its configured
 * hot regions, but it cannot allocate, grow, create files, or prefetch cold
 * regions.
 */
#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <poll.h>
#include <signal.h>
#include <stdarg.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/un.h>
#include <sys/types.h>
#include <time.h>
#include <unistd.h>

enum ballast_state {
    CREATED, FOREGROUND_ALLOCATING, FOREGROUND_ACTIVE, BACKGROUND_IDLE,
    REACCESSING, VERIFYING, STOPPED, ERROR_STATE,
};

struct region {
    const char *name;
    size_t bytes;
    uint64_t expected_checksum;
    uint64_t accesses;
    uint64_t last_latency_us;
    off_t file_offset;
    unsigned char *anon;
};

struct ballast {
    const char *app_key;
    const char *socket_path;
    const char *log_path;
    const char *file_path;
    FILE *log;
    int listen_fd;
    int file_fd;
    long page_size;
    int hot_interval_ms;
    enum ballast_state state;
    bool allocated;
    bool stopping;
    uint64_t background_since_ns;
    uint64_t next_hot_ns;
    struct region anon_cold, anon_hot, file_cold, file_hot;
};

static volatile sig_atomic_t g_stop = 0;

static void on_signal(int signum) { (void)signum; g_stop = 1; }

static uint64_t now_ns(void) {
    struct timespec ts;
    clock_gettime(CLOCK_REALTIME, &ts);
    return (uint64_t)ts.tv_sec * 1000000000ull + (uint64_t)ts.tv_nsec;
}

static uint64_t mono_ns(void) {
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (uint64_t)ts.tv_sec * 1000000000ull + (uint64_t)ts.tv_nsec;
}

static const char *state_name(enum ballast_state state) {
    static const char *names[] = {
        "CREATED", "FOREGROUND_ALLOCATING", "FOREGROUND_ACTIVE",
        "BACKGROUND_IDLE", "REACCESSING", "VERIFYING", "STOPPED", "ERROR",
    };
    return (state >= CREATED && state <= ERROR_STATE) ? names[state] : "ERROR";
}

static void csv_log(struct ballast *b, enum ballast_state before,
                    enum ballast_state after, const char *command,
                    uint64_t latency_us, const char *status, const char *error) {
    if (!b->log) return;
    /* Command/status/error are controlled tokens: never permit commas/newlines. */
    fprintf(b->log,
        "%llu,%s,%ld,%s,%s,%s,%zu,%zu,%zu,%zu,%llu,%s,%s\n",
        (unsigned long long)now_ns(), b->app_key, (long)getpid(),
        state_name(before), state_name(after), command,
        b->anon_cold.bytes, b->anon_hot.bytes, b->file_cold.bytes, b->file_hot.bytes,
        (unsigned long long)latency_us, status, error ? error : "");
    fflush(b->log);
}

static uint64_t mix(uint64_t value, uint64_t item) {
    value ^= item + 0x9e3779b97f4a7c15ull + (value << 6) + (value >> 2);
    return value;
}

static unsigned char byte_pattern(const char *name, uint64_t offset) {
    uint64_t value = 0xcbf29ce484222325ull;
    for (const unsigned char *p = (const unsigned char *)name; *p; ++p)
        value = mix(value, *p);
    value = mix(value, offset);
    return (unsigned char)(value & 0xffu);
}

static uint64_t touch_anon(struct ballast *b, struct region *r, bool write) {
    uint64_t sum = 0;
    for (size_t offset = 0; offset < r->bytes; offset += (size_t)b->page_size) {
        unsigned char value = byte_pattern(r->name, offset);
        if (write) r->anon[offset] = value;
        sum = mix(sum, r->anon[offset]);
        r->accesses++;
    }
    return sum;
}

static int write_all(int fd, const unsigned char *buffer, size_t length, off_t offset) {
    size_t done = 0;
    while (done < length) {
        ssize_t rc = pwrite(fd, buffer + done, length - done, offset + (off_t)done);
        if (rc < 0) { if (errno == EINTR) continue; return -1; }
        if (rc == 0) { errno = EIO; return -1; }
        done += (size_t)rc;
    }
    return 0;
}

static uint64_t read_file_region(struct ballast *b, struct region *r) {
    unsigned char buffer[65536];
    uint64_t sum = 0;
    size_t done = 0;
    while (done < r->bytes) {
        size_t want = r->bytes - done;
        if (want > sizeof(buffer)) want = sizeof(buffer);
        ssize_t rc = pread(b->file_fd, buffer, want, r->file_offset + (off_t)done);
        if (rc <= 0) return UINT64_MAX;
        for (ssize_t index = 0; index < rc; ++index)
            sum = mix(sum, buffer[index]);
        done += (size_t)rc;
        r->accesses += (uint64_t)((rc + b->page_size - 1) / b->page_size);
    }
    return sum;
}

static int create_and_fill_file(struct ballast *b) {
    unsigned char buffer[65536];
    struct region *regions[] = { &b->file_cold, &b->file_hot };
    b->file_fd = open(b->file_path, O_CREAT | O_EXCL | O_RDWR | O_CLOEXEC, 0600);
    if (b->file_fd < 0) return -1;
    off_t offset = 0;
    for (size_t index = 0; index < 2; ++index) {
        struct region *r = regions[index];
        r->file_offset = offset;
        size_t done = 0;
        while (done < r->bytes) {
            size_t amount = r->bytes - done;
            if (amount > sizeof(buffer)) amount = sizeof(buffer);
            for (size_t byte = 0; byte < amount; ++byte)
                buffer[byte] = byte_pattern(r->name, done + byte);
            if (write_all(b->file_fd, buffer, amount, offset + (off_t)done) < 0) return -1;
            done += amount;
        }
        offset += (off_t)r->bytes;
    }
    if (fsync(b->file_fd) < 0) return -1;
    b->file_cold.expected_checksum = read_file_region(b, &b->file_cold);
    b->file_hot.expected_checksum = read_file_region(b, &b->file_hot);
    return (b->file_cold.expected_checksum == UINT64_MAX ||
            b->file_hot.expected_checksum == UINT64_MAX) ? -1 : 0;
}

static int allocate(struct ballast *b, char *error, size_t error_size) {
    if (b->state != FOREGROUND_ACTIVE) {
        snprintf(error, error_size, "ALLOCATE_REQUIRES_FOREGROUND_ACTIVE"); return -1;
    }
    if (b->allocated) { snprintf(error, error_size, "ALREADY_ALLOCATED"); return -1; }
    enum ballast_state before = b->state;
    b->state = FOREGROUND_ALLOCATING;
    uint64_t started = mono_ns();
    struct region *anon[] = { &b->anon_cold, &b->anon_hot };
    for (size_t index = 0; index < 2; ++index) {
        struct region *r = anon[index];
        if (!r->bytes) continue;
        r->anon = mmap(NULL, r->bytes, PROT_READ | PROT_WRITE,
                       MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
        if (r->anon == MAP_FAILED) { r->anon = NULL; goto failed; }
        r->expected_checksum = touch_anon(b, r, true);
    }
    if (create_and_fill_file(b) < 0) goto failed;
    b->allocated = true;
    b->state = FOREGROUND_ACTIVE;
    csv_log(b, before, b->state, "ALLOCATE", (mono_ns() - started) / 1000, "OK", "");
    return 0;
failed:
    snprintf(error, error_size, "ALLOCATE_%s", strerror(errno));
    b->state = ERROR_STATE;
    csv_log(b, before, b->state, "ALLOCATE", (mono_ns() - started) / 1000, "ERROR", error);
    return -1;
}

static int check_region(struct ballast *b, struct region *r, bool is_file,
                        const char *command, char *error, size_t error_size) {
    if (!b->allocated || b->state != FOREGROUND_ACTIVE) {
        snprintf(error, error_size, "%s_REQUIRES_FOREGROUND_ACTIVE", command); return -1;
    }
    enum ballast_state before = b->state;
    b->state = (strcmp(command, "VERIFY") == 0) ? VERIFYING : REACCESSING;
    uint64_t started = mono_ns();
    uint64_t checksum = is_file ? read_file_region(b, r) : touch_anon(b, r, false);
    r->last_latency_us = (mono_ns() - started) / 1000;
    if (checksum == UINT64_MAX || checksum != r->expected_checksum) {
        snprintf(error, error_size, "CHECKSUM_MISMATCH_%s", r->name);
        b->state = ERROR_STATE;
        csv_log(b, before, b->state, command, r->last_latency_us, "ERROR", error);
        return -1;
    }
    b->state = FOREGROUND_ACTIVE;
    csv_log(b, before, b->state, command, r->last_latency_us, "OK", r->name);
    return 0;
}

static int verify_all(struct ballast *b, char *error, size_t error_size) {
    if (!b->allocated || b->state != FOREGROUND_ACTIVE) {
        snprintf(error, error_size, "VERIFY_REQUIRES_FOREGROUND_ACTIVE"); return -1;
    }
    enum ballast_state before = b->state;
    b->state = VERIFYING;
    uint64_t started = mono_ns();
    uint64_t checks[] = {
        read_file_region(b, &b->file_hot), touch_anon(b, &b->anon_hot, false),
        read_file_region(b, &b->file_cold), touch_anon(b, &b->anon_cold, false),
    };
    uint64_t expected[] = { b->file_hot.expected_checksum, b->anon_hot.expected_checksum,
                            b->file_cold.expected_checksum, b->anon_cold.expected_checksum };
    for (size_t i = 0; i < 4; ++i) if (checks[i] == UINT64_MAX || checks[i] != expected[i]) {
        snprintf(error, error_size, "VERIFY_CHECKSUM_MISMATCH"); b->state = ERROR_STATE;
        csv_log(b, before, b->state, "VERIFY", (mono_ns()-started)/1000, "ERROR", error); return -1;
    }
    b->state = FOREGROUND_ACTIVE;
    csv_log(b, before, b->state, "VERIFY", (mono_ns()-started)/1000, "OK", "");
    return 0;
}

static void background_hot_tick(struct ballast *b) {
    if (!b->allocated || b->state != BACKGROUND_IDLE || b->hot_interval_ms <= 0) return;
    uint64_t now = mono_ns();
    if (now < b->next_hot_ns) return;
    /* Only one existing page from each hot region is touched.  Scanning an
     * entire hot region every second made the sidecar itself a background
     * memory workload, which defeats Test4B's cold-page construction goal. */
    if (b->anon_hot.anon && b->anon_hot.bytes) {
        size_t pages = (b->anon_hot.bytes + (size_t)b->page_size - 1) / (size_t)b->page_size;
        size_t offset = (size_t)(b->anon_hot.accesses % pages) * (size_t)b->page_size;
        volatile unsigned char value = b->anon_hot.anon[offset];
        (void)value;
        b->anon_hot.accesses++;
    }
    if (b->file_fd >= 0 && b->file_hot.bytes) {
        unsigned char page[4096];
        size_t pages = (b->file_hot.bytes + sizeof(page) - 1) / sizeof(page);
        off_t offset = b->file_hot.file_offset + (off_t)((b->file_hot.accesses % pages) * sizeof(page));
        ssize_t read_bytes = pread(b->file_fd, page, sizeof(page), offset);
        if (read_bytes > 0) b->file_hot.accesses++;
    }
    b->next_hot_ns = now + (uint64_t)b->hot_interval_ms * 1000000ull;
    csv_log(b, BACKGROUND_IDLE, BACKGROUND_IDLE, "BACKGROUND_HOT_TICK", 0, "OK", "");
}

static void send_response(int client, const char *format, ...) {
    char out[1024]; va_list args;
    va_start(args, format); vsnprintf(out, sizeof(out), format, args); va_end(args);
    ssize_t ignored = write(client, out, strlen(out));
    (void)ignored;
}

static void command(struct ballast *b, int client, char *line) {
    char *newline = strpbrk(line, "\r\n"); if (newline) *newline = '\0';
    char error[160] = ""; enum ballast_state before = b->state;
    if (!strcmp(line, "STATUS")) {
        send_response(client, "OK state=%s allocated=%d pid=%ld background_since_ns=%llu anon_cold=%zu anon_hot=%zu file_cold=%zu file_hot=%zu hot_accesses=%llu cold_accesses=%llu\n",
            state_name(b->state), b->allocated ? 1 : 0, (long)getpid(),
            (unsigned long long)b->background_since_ns, b->anon_cold.bytes, b->anon_hot.bytes,
            b->file_cold.bytes, b->file_hot.bytes,
            (unsigned long long)(b->anon_hot.accesses + b->file_hot.accesses),
            (unsigned long long)(b->anon_cold.accesses + b->file_cold.accesses));
        csv_log(b, before, before, "STATUS", 0, "OK", ""); return;
    }
    if (!strcmp(line, "ENTER_FOREGROUND")) {
        if (b->state == ERROR_STATE || b->state == STOPPED) { snprintf(error, sizeof(error), "INVALID_STATE"); }
        else { b->state = FOREGROUND_ACTIVE; b->background_since_ns = 0; }
    } else if (!strcmp(line, "ALLOCATE")) {
        if (allocate(b, error, sizeof(error)) == 0) { send_response(client, "OK state=%s allocated=1\n", state_name(b->state)); return; }
    } else if (!strcmp(line, "ENTER_BACKGROUND")) {
        if (!b->allocated || b->state != FOREGROUND_ACTIVE) snprintf(error, sizeof(error), "BACKGROUND_REQUIRES_ALLOCATED_FOREGROUND");
        else { b->state = BACKGROUND_IDLE; b->background_since_ns = now_ns(); b->next_hot_ns = mono_ns() + (uint64_t)b->hot_interval_ms * 1000000ull; }
    } else if (!strcmp(line, "REACCESS_HOT")) {
        if (check_region(b, &b->file_hot, true, "REACCESS_HOT_FILE", error, sizeof(error)) == 0 &&
            check_region(b, &b->anon_hot, false, "REACCESS_HOT_ANON", error, sizeof(error)) == 0) { send_response(client, "OK state=%s\n", state_name(b->state)); return; }
    } else if (!strcmp(line, "REACCESS_COLD")) {
        if (check_region(b, &b->file_cold, true, "REACCESS_COLD_FILE", error, sizeof(error)) == 0 &&
            check_region(b, &b->anon_cold, false, "REACCESS_COLD_ANON", error, sizeof(error)) == 0) { send_response(client, "OK state=%s\n", state_name(b->state)); return; }
    } else if (!strcmp(line, "VERIFY")) {
        if (verify_all(b, error, sizeof(error)) == 0) { send_response(client, "OK state=%s\n", state_name(b->state)); return; }
    } else if (!strcmp(line, "STOP")) {
        b->state = STOPPED; b->stopping = true; csv_log(b, before, b->state, "STOP", 0, "OK", ""); send_response(client, "OK stopped=1\n"); return;
    } else {
        snprintf(error, sizeof(error), "UNKNOWN_COMMAND");
    }
    if (error[0]) {
        csv_log(b, before, b->state, line, 0, "REJECTED", error);
        send_response(client, "ERR reason=%s state=%s\n", error, state_name(b->state));
    } else {
        csv_log(b, before, b->state, line, 0, "OK", "");
        send_response(client, "OK state=%s allocated=%d\n", state_name(b->state), b->allocated ? 1 : 0);
    }
}

static size_t parse_size(const char *value, const char *name) {
    char *end = NULL; errno = 0; unsigned long long amount = strtoull(value, &end, 10);
    if (errno || !end || *end) { fprintf(stderr, "invalid %s: %s\n", name, value); exit(2); }
    return (size_t)amount;
}

static void usage(const char *program) {
    fprintf(stderr, "Usage: %s --app-key APP --socket PATH --log PATH --file PATH [--anon-cold BYTES --anon-hot BYTES --file-cold BYTES --file-hot BYTES --hot-interval-ms MS]\n", program);
}

int main(int argc, char **argv) {
    struct ballast b = { .listen_fd = -1, .file_fd = -1, .page_size = sysconf(_SC_PAGESIZE), .hot_interval_ms = 1000, .state = CREATED };
    b.anon_cold.name="anon_cold"; b.anon_hot.name="anon_hot"; b.file_cold.name="file_cold"; b.file_hot.name="file_hot";
    b.anon_cold.bytes=32u*1024u*1024u; b.anon_hot.bytes=8u*1024u*1024u; b.file_cold.bytes=48u*1024u*1024u; b.file_hot.bytes=8u*1024u*1024u;
    for (int i=1; i<argc; i+=2) {
        if (i+1 >= argc) { usage(argv[0]); return 2; }
        if (!strcmp(argv[i], "--app-key")) b.app_key=argv[i+1];
        else if (!strcmp(argv[i], "--socket")) b.socket_path=argv[i+1];
        else if (!strcmp(argv[i], "--log")) b.log_path=argv[i+1];
        else if (!strcmp(argv[i], "--file")) b.file_path=argv[i+1];
        else if (!strcmp(argv[i], "--anon-cold")) b.anon_cold.bytes=parse_size(argv[i+1], argv[i]);
        else if (!strcmp(argv[i], "--anon-hot")) b.anon_hot.bytes=parse_size(argv[i+1], argv[i]);
        else if (!strcmp(argv[i], "--file-cold")) b.file_cold.bytes=parse_size(argv[i+1], argv[i]);
        else if (!strcmp(argv[i], "--file-hot")) b.file_hot.bytes=parse_size(argv[i+1], argv[i]);
        else if (!strcmp(argv[i], "--hot-interval-ms")) b.hot_interval_ms=(int)parse_size(argv[i+1], argv[i]);
        else { usage(argv[0]); return 2; }
    }
    if (!b.app_key || !b.socket_path || !b.log_path || !b.file_path || b.page_size <= 0) { usage(argv[0]); return 2; }
    b.log = fopen(b.log_path, "w"); if (!b.log) { perror("fopen log"); return 1; }
    fprintf(b.log, "timestamp_ns,app_key,pid,state_before,state_after,command,anon_cold_bytes,anon_hot_bytes,file_cold_bytes,file_hot_bytes,operation_latency_us,status,error\n"); fflush(b.log);
    if (strlen(b.socket_path) >= sizeof(((struct sockaddr_un *)0)->sun_path)) { fprintf(stderr, "socket path too long\n"); return 2; }
    unlink(b.socket_path);
    b.listen_fd = socket(AF_UNIX, SOCK_STREAM | SOCK_CLOEXEC, 0); if (b.listen_fd < 0) { perror("socket"); return 1; }
    struct sockaddr_un addr; memset(&addr, 0, sizeof(addr)); addr.sun_family=AF_UNIX; strncpy(addr.sun_path, b.socket_path, sizeof(addr.sun_path)-1);
    if (bind(b.listen_fd, (struct sockaddr *)&addr, sizeof(addr)) < 0 || listen(b.listen_fd, 8) < 0) { perror("bind/listen"); return 1; }
    chmod(b.socket_path, 0600); signal(SIGINT, on_signal); signal(SIGTERM, on_signal);
    csv_log(&b, CREATED, CREATED, "START", 0, "OK", "");
    while (!g_stop && !b.stopping) {
        struct pollfd pfd = { .fd=b.listen_fd, .events=POLLIN }; int rc=poll(&pfd, 1, 100);
        if (rc > 0 && (pfd.revents & POLLIN)) { int client=accept4(b.listen_fd, NULL, NULL, SOCK_CLOEXEC); if (client >= 0) { char line[256]={0}; ssize_t n=read(client,line,sizeof(line)-1); if(n>0) command(&b,client,line); close(client); } }
        background_hot_tick(&b);
    }
    if (b.state != STOPPED) { enum ballast_state before=b.state; b.state=STOPPED; csv_log(&b,before,b.state,"SIGNAL_STOP",0,"OK",""); }
    if (b.anon_cold.anon) munmap(b.anon_cold.anon,b.anon_cold.bytes);
    if (b.anon_hot.anon) munmap(b.anon_hot.anon,b.anon_hot.bytes);
    if (b.file_fd >= 0) close(b.file_fd);
    if (b.listen_fd >= 0) close(b.listen_fd);
    unlink(b.socket_path);
    fclose(b.log);
    return 0;
}
