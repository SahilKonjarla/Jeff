/* Standalone, freestanding Linux PID1 helper SOURCE; not wired to Jeff.
 * Compile-only objects are not runnable guard artifacts or kernel safety proof.
 * Invocation: guard --jfg1 DIGEST STARTUP_NS RUNTIME_NS RATE_PPM MARGIN_NS -- /exec args...
 * Host transport/manifest/clock assumptions still require separate verification.
 */
#include "guard_abi.h"

__attribute__((used,section(".jeff.guard")))
const g_u8 jeff_guard_abi[] = {'J','G','O','1',G_WIRE,8,1,G_ABI,
                              G_NONCE,G_DIGEST,G_WORDS,G_WORDS+8,G_WORDS+16,1,0,0};

__attribute__((noreturn)) static void g_exit(unsigned code) {
    G_CALL(exit_group,code,0,0,0,0,0);
    for (;;) __builtin_trap(); /* Cannot turn a refused exit into successful work. */
}

__attribute__((naked,noreturn)) static void g_restore(void) {
#if defined(__aarch64__)
    __asm__ volatile("mov x8, #139\nsvc #0\nbrk #0");
#else
    __asm__ volatile("mov $15, %rax\nsyscall\nud2");
#endif
}

__attribute__((noreturn)) static void g_term(int signal) {
    (void)signal;
    g_exit(125); /* Catchable child-origin TERM can abort, never renew or succeed. */
}

static g_u64 g_now(void) {
    struct g_timespec value;
    if (G_CALL(clock_gettime,7,&value,0,0,0,0) < 0 || value.seconds < 0 ||
        value.nanos < 0 || value.nanos >= 1000000000 ||
        (g_u64)value.seconds > (G_MAX_NS-(g_u64)value.nanos)/1000000000) g_exit(126);
    return (g_u64)value.seconds*1000000000 + (g_u64)value.nanos;
}

static void g_check_tick(struct g_guard *guard) {
    unsigned state = guard->state;
    if (!g_tick(guard,g_now())) g_exit(state == 1 ? 124 : 125);
}

static void g_pause(int fd, g_u16 events, const struct g_guard *guard) {
    g_u64 now = g_now();
    if (now < guard->last || now >= guard->deadline) return;
    g_u64 left = guard->deadline-now;
    struct g_timespec timeout = {0,(g_i64)(left < 100000000 ? left : 100000000)};
    struct g_pollfd descriptor = {fd,events,0};
    long result = G_CALL(ppoll,&descriptor,1,&timeout,0,8,0);
    if (result < 0 && result != -4) g_exit(125);
}

static void g_startup_checks(void) {
    g_u32 identities[3];
    struct { g_u32 version; int pid; } header = {0x20080522U,0};
    struct { g_u32 effective, permitted, inheritable; } caps[2];
    if (G_CALL(getpid,0,0,0,0,0,0) != 1 ||
        G_CALL(getresuid,&identities[0],&identities[1],&identities[2],0,0,0) < 0 ||
        identities[0] != 65532 || identities[1] != 65532 || identities[2] != 65532) g_exit(126);
    if (G_CALL(getresgid,&identities[0],&identities[1],&identities[2],0,0,0) < 0 ||
        identities[0] != 65532 || identities[1] != 65532 || identities[2] != 65532) g_exit(126);
    if (G_CALL(capget,&header,caps,0,0,0,0) < 0 ||
        caps[0].effective || caps[0].permitted || caps[0].inheritable ||
        caps[1].effective || caps[1].permitted || caps[1].inheritable ||
        G_CALL(prctl,39,0,0,0,0,0) != 1 || /* PR_GET_NO_NEW_PRIVS */
        G_CALL(prctl,21,0,0,0,0,0) != 2 || /* mode, NOT profile-content verification */
        G_CALL(prctl,4,0,0,0,0,0) < 0 ||  /* PR_SET_DUMPABLE=0 after final exec */
        G_CALL(prctl,3,0,0,0,0,0) != 0) g_exit(126);
    long groups = G_CALL(getgroups,0,0,0,0,0,0);
    if (groups < 0 || groups > 1 || (groups == 1 &&
        (G_CALL(getgroups,1,&identities[0],0,0,0,0) != 1 || identities[0] != 65532))) g_exit(126);
    if (G_CALL(close_range,3,0xffffffffU,0,0,0,0) < 0) g_exit(126);
    struct g_action action = {g_term,0x04000000UL,g_restore,0};
    struct g_action defaults = {0,0,0,0};
    g_u64 empty_mask = 0;
    if (G_CALL(rt_sigaction,15,&action,0,8,0,0) < 0 ||
        G_CALL(rt_sigaction,17,&defaults,0,8,0,0) < 0 ||
        G_CALL(rt_sigprocmask,2,&empty_mask,0,8,0,0) < 0) g_exit(126);
}

static int g_literal(const char *actual, const char *expected) {
    while (*expected) if (*actual++ != *expected++) return 0;
    return *actual == 0;
}

static int g_arguments(g_u64 argc, char **argv) {
    if (argc < 9 || argc > 72 || !g_literal(argv[1],"--jfg1") ||
        !g_literal(argv[7],"--") || argv[8][0] != '/' || argv[8][1] == '/') return 0;
    unsigned total = 0;
    for (g_u64 i = 8; i < argc; ++i) {
        unsigned size = 0;
        while (argv[i][size]) {
            unsigned char value = (unsigned char)argv[i][size];
            if (value < 32 || value == 127 || ++size > 4096 || ++total > 8192) return 0;
        }
    }
    const char *path = argv[8];
    for (unsigned i = 0; path[i]; ++i) {
        if (path[i] == '/' && path[i+1] == '.' && path[i+2] == '.' &&
            (!path[i+3] || path[i+3] == '/')) return 0;
    }
    return 1;
}

static long g_checked_stdio_flags(int fd, unsigned long required) {
    long flags = G_CALL(fcntl,fd,3,0,0,0,0); /* F_GETFL */
    if (!G_IO_ACCESS_OK(flags,required)) g_exit(126);
    return flags;
}

static void g_nonblocking(int fd, long checked_flags) {
    if (G_CALL(fcntl,fd,4,checked_flags|0x800,0,0,0) < 0) g_exit(126);
}

static void g_send_challenge(struct g_guard *guard) {
    g_u8 bytes[G_WIRE];
    g_challenge(guard,bytes);
    unsigned offset = 0;
    while (offset < G_WIRE) {
        g_check_tick(guard);
        long count = G_CALL(write,1,bytes+offset,G_WIRE-offset,0,0,0);
        if (count > 0 && (g_u64)count <= G_WIRE-offset) offset += (unsigned)count;
        else if (count == -11) g_pause(1,4,guard); /* EAGAIN/POLLOUT */
        else if (count != -4) g_exit(125);
    }
}

static void g_receive_authorization(struct g_guard *guard) {
    g_u8 bytes[G_WIRE];
    unsigned offset = 0;
    while (offset < G_WIRE) {
        g_check_tick(guard);
        long count = G_CALL(read,0,bytes+offset,G_WIRE-offset,0,0,0);
        if (count > 0 && (g_u64)count <= G_WIRE-offset) offset += (unsigned)count;
        else if (count == -11) g_pause(0,1,guard); /* EAGAIN/POLLIN */
        else if (count != -4) g_exit(125); /* EOF, errors, malformed transport */
    }
    if (!g_arm(guard,bytes,g_now())) g_exit(125);
}

__attribute__((noreturn)) static void g_child(char **argv, char **environment) {
    struct g_action action = {0,0,0,0};
    if (G_CALL(rt_sigaction,15,&action,0,8,0,0) < 0) g_exit(126);
    long input = G_CALL(openat,-100,"/dev/null",0x80000,0,0,0); /* RDONLY|CLOEXEC */
    if (input < 3 || G_CALL(dup3,input,0,0,0,0,0) < 0 ||
        G_CALL(close_range,3,0xffffffffU,0,0,0,0) < 0) g_exit(126);
    struct g_program policy = {
        (g_u16)(sizeof(g_child_policy)/sizeof(g_child_policy[0])),g_child_policy
    };
    if (G_CALL(prctl,38,1,0,0,0,0) < 0 || /* PR_SET_NO_NEW_PRIVS */
        G_CALL(seccomp,1,0,&policy,0,0,0) < 0) g_exit(126);
    G_CALL(execve,argv[0],argv,environment,0,0,0);
    g_exit(127); /* No PATH, shell, interpreter or native fallback. */
}

__attribute__((noreturn)) static void g_watch(struct g_guard *guard, long primary) {
    for (;;) {
        g_check_tick(guard);
        g_u8 byte;
        long input = G_CALL(read,0,&byte,1,0,0,0);
        if (input >= 0 || (input != -11 && input != -4)) g_exit(125);
        /* Bound reaping even if the job continuously creates/exits descendants. */
        for (unsigned i = 0; i < 64; ++i) {
            int status = 0;
            g_check_tick(guard);
            long child = G_CALL(wait4,-1,&status,1,0,0,0); /* WNOHANG */
            if (child == primary) {
                g_check_tick(guard);
                unsigned signal = (unsigned)status & 127;
                g_exit(signal ? 128+signal : ((unsigned)status >> 8)&255);
            }
            if (child == 0 || child == -4) break;
            if (child < 0) g_exit(125);
        }
        g_pause(0,1,guard);
    }
}

__attribute__((used,visibility("hidden"),noreturn))
void guard_main(g_u64 *stack) {
    g_u64 argc = stack[0];
    if (argc < 9 || argc > 72) g_exit(126);
    char **argv = (char **)(stack+1);
    char **environment = argv+argc+1;
    if (!g_arguments(argc,argv)) g_exit(126);
    g_startup_checks();
    struct g_guard guard;
    g_u64 startup, runtime, rate, margin;
    if (!g_hex_digest(argv[2],guard.digest) || !g_decimal(argv[3],&startup) ||
        !g_decimal(argv[4],&runtime) || !g_decimal(argv[5],&rate) ||
        !g_decimal(argv[6],&margin)) g_exit(126);
    if (G_CALL(getrandom,guard.nonce,16,1,0,0,0) != 16 || /* NONBLOCK: no indefinite entropy wait */
        !g_initialize(&guard,g_now(),startup,runtime,rate,margin)) g_exit(126);
    /* Validate all modes BEFORE F_SETFL: shared OFDs must not let workload
     * output flag changes make the guard's control read blocking.
     * Read-write sockets/TTYs are deliberately unsupported; no fallback.
     */
    long input_flags = g_checked_stdio_flags(0,G_CONTROL_ACCESS);
    long output_flags = g_checked_stdio_flags(1,G_OUTPUT_ACCESS);
    g_checked_stdio_flags(2,G_OUTPUT_ACCESS);
    g_nonblocking(0,input_flags);
    g_nonblocking(1,output_flags);
    g_send_challenge(&guard);
    g_receive_authorization(&guard);
    g_check_tick(&guard);
    if (G_CALL(fcntl,1,4,output_flags,0,0,0) < 0) g_exit(126);
    /* No guard output after this point: blocked workload pipes cannot block PID1. */
    g_check_tick(&guard);
    long child = G_CALL(clone,17,0,0,0,0,0); /* Separate address space, SIGCHLD. */
    if (child < 0) g_exit(126);
    if (child == 0) g_child(argv+8,environment);
    g_watch(&guard,child);
}

__attribute__((naked,noreturn)) void _start(void) {
#if defined(__aarch64__)
    __asm__ volatile("mov x0, sp\nbl guard_main\nbrk #0");
#else
    __asm__ volatile("mov %rsp, %rdi\nand $-16, %rsp\ncall guard_main\nud2");
#endif
}
