/* JFG1 source contract. No OS calls, allocator, transport, or launch authority.
 * Wire layout mirrors application/execution/guard_protocol.py; state is local only.
 */
#ifndef JEFF_GUARD_CORE_H
#define JEFF_GUARD_CORE_H

typedef __UINT8_TYPE__ g_u8;
typedef __UINT16_TYPE__ g_u16;
typedef __UINT32_TYPE__ g_u32;
typedef __UINT64_TYPE__ g_u64;
typedef __INT64_TYPE__ g_i64;
typedef __SIZE_TYPE__ g_size;

#define G_WIRE 77
#define G_NONCE 5
#define G_DIGEST 21
#define G_WORDS 53
#define G_MAX_NS 9223372036854775807ULL
#define G_MAX_RUNTIME 3600000000000ULL

/* Access mode belongs to the OFD and cannot be changed with F_SETFL.
 * Disjoint read-only control / write-only output modes exclude shared OFDs.
 * Pass checked local values, never expressions with side effects, to this predicate.
 */
#define G_ACCESS_MASK 3UL
#define G_CONTROL_ACCESS 0UL
#define G_OUTPUT_ACCESS 1UL
#define G_IO_ACCESS_OK(flags, required) \
    ((flags) >= 0 && (((unsigned long)(flags) & G_ACCESS_MASK) == (required)))

struct g_guard {
    g_u8 nonce[16], digest[32];
    g_u64 issued, startup, runtime, maximum, deadline, last;
    unsigned state; /* 0 parked, 1 armed (not start evidence), 2 exit required */
};

static inline int g_equal(const g_u8 *a, const g_u8 *b, g_size size) {
    unsigned difference = 0;
    for (g_size i = 0; i < size; ++i) difference |= a[i] ^ b[i];
    return difference == 0;
}

static inline int g_nonzero(const g_u8 *bytes, g_size size) {
    unsigned value = 0;
    for (g_size i = 0; i < size; ++i) value |= bytes[i];
    return value != 0;
}

static inline int g_decimal(const char *text, g_u64 *result) {
    g_u64 number = 0;
    if (!*text || (*text == '0' && text[1])) return 0;
    for (unsigned i = 0; text[i]; ++i) {
        if (i >= 19 || text[i] < '0' || text[i] > '9') return 0;
        unsigned digit = (unsigned)(text[i] - '0');
        if (number > (G_MAX_NS - digit) / 10) return 0;
        number = number * 10 + digit;
    }
    *result = number;
    return 1;
}

static inline int g_hex_digest(const char *text, g_u8 *bytes) {
    for (unsigned i = 0; i < 64; ++i) {
        unsigned char value = (unsigned char)text[i];
        unsigned digit;
        if (value >= '0' && value <= '9') digit = value - '0';
        else if (value >= 'a' && value <= 'f') digit = value - 'a' + 10;
        else return 0;
        if (i % 2) bytes[i/2] |= (g_u8)digit;
        else bytes[i/2] = (g_u8)(digit << 4);
    }
    return text[64] == 0;
}

static inline void g_put64(g_u8 *bytes, g_u64 value) {
    for (unsigned i = 0; i < 8; ++i) bytes[7-i] = (g_u8)(value >> (i*8));
}

static inline g_u64 g_get64(const g_u8 *bytes) {
    g_u64 value = 0;
    for (unsigned i = 0; i < 8; ++i) value = (value << 8) | bytes[i];
    return value;
}

static inline int g_initialize(struct g_guard *guard, g_u64 now,
                                g_u64 startup, g_u64 runtime,
                                g_u64 rate_floor, g_u64 margin) {
    if (!startup || startup > runtime || !runtime || runtime > G_MAX_RUNTIME ||
        !rate_floor || rate_floor > 1000000 || !margin || margin >= runtime ||
        now > G_MAX_NS - runtime || !g_nonzero(guard->nonce, 16)) return 0;
    g_u64 duration = (runtime - margin) * rate_floor / 1000000;
    if (!duration) return 0;
    guard->issued = guard->last = now;
    guard->startup = guard->deadline = now + startup;
    guard->runtime = runtime;
    guard->maximum = now + duration;
    guard->state = 0;
    return 1;
}

static inline void g_challenge(const struct g_guard *guard, g_u8 *bytes) {
    bytes[0] = 'J'; bytes[1] = 'F'; bytes[2] = 'G'; bytes[3] = '1'; bytes[4] = 1;
    for (unsigned i = 0; i < 16; ++i) bytes[G_NONCE+i] = guard->nonce[i];
    for (unsigned i = 0; i < 32; ++i) bytes[G_DIGEST+i] = guard->digest[i];
    g_put64(bytes+G_WORDS, guard->issued);
    g_put64(bytes+G_WORDS+8, guard->startup);
    g_put64(bytes+G_WORDS+16, guard->runtime);
}

static inline int g_tick(struct g_guard *guard, g_u64 now) {
    if (guard->state == 2) return 0;
    if (now > G_MAX_NS || now < guard->last || now >= guard->deadline) {
        guard->state = 2;
        return 0;
    }
    guard->last = now;
    return 1;
}

static inline int g_arm(struct g_guard *guard, const g_u8 *bytes, g_u64 now) {
    if (!g_tick(guard, now)) return 0;
    if (guard->state != 0 || bytes[0] != 'J' || bytes[1] != 'F' ||
        bytes[2] != 'G' || bytes[3] != '1' || bytes[4] != 2 ||
        !g_equal(bytes+G_NONCE, guard->nonce, 16) ||
        !g_equal(bytes+G_DIGEST, guard->digest, 32) ||
        g_get64(bytes+G_WORDS+8) || g_get64(bytes+G_WORDS+16)) {
        guard->state = 2;
        return 0;
    }
    g_u64 deadline = g_get64(bytes+G_WORDS);
    if (deadline <= now || deadline > guard->maximum) {
        guard->state = 2;
        return 0;
    }
    guard->deadline = deadline;
    guard->state = 1;
    return 1;
}
#endif
