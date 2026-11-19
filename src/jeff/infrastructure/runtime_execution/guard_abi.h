/* Native LP64 Linux ABI only. Reference: Linux v6.12 UAPI asm-generic/unistd.h,
 * arch/x86/entry/syscalls/syscall_64.tbl, signal.h, seccomp.h, audit.h.
 * Interface constants/layouts, not a vendored sysroot or libc implementation.
 */
#ifndef JEFF_GUARD_ABI_H
#define JEFF_GUARD_ABI_H
#include "guard_core.h"
#if !defined(__linux__) || !defined(__LP64__) || __BYTE_ORDER__ != __ORDER_LITTLE_ENDIAN__
#error "Guard requires native little-endian LP64 Linux"
#endif
_Static_assert(sizeof(long) == 8 && sizeof(void *) == 8, "LP64 ABI required");

#if defined(__aarch64__)
#define G_ABI 1
#define G_AUDIT 0xc00000b7U
#define G_NR_read 63
#define G_NR_write 64
#define G_NR_close 57
#define G_NR_openat 56
#define G_NR_dup3 24
#define G_NR_fcntl 25
#define G_NR_ppoll 73
#define G_NR_clock_gettime 113
#define G_NR_getpid 172
#define G_NR_getresuid 148
#define G_NR_getresgid 150
#define G_NR_capget 90
#define G_NR_prctl 167
#define G_NR_rt_sigaction 134
#define G_NR_rt_sigprocmask 135
#define G_NR_getgroups 158
#define G_NR_getrandom 278
#define G_NR_clone 220
#define G_NR_wait4 260
#define G_NR_execve 221
#define G_NR_exit_group 94
#define G_NR_seccomp 277
#define G_NR_ptrace 117
#define G_NR_vm_read 270
#define G_NR_vm_write 271
#define G_NR_setaffinity 122
#define G_NR_setattr 274
#define G_NR_setparam 118
#define G_NR_setscheduler 119
#define G_NR_setpriority 140
#define G_NR_prlimit 261
#define G_NR_setns 268
#define G_NR_unshare 97
#define G_NR_kcmp 272
#define G_NR_perf 241
#define G_NR_bpf 280
#define G_NR_userfaultfd 282
static inline long g_syscall(long nr, long a, long b, long c, long d, long e, long f) {
    register long x8 __asm__("x8") = nr;
    register long x0 __asm__("x0") = a, x1 __asm__("x1") = b, x2 __asm__("x2") = c;
    register long x3 __asm__("x3") = d, x4 __asm__("x4") = e, x5 __asm__("x5") = f;
    __asm__ volatile("svc #0" : "+r"(x0) : "r"(x8), "r"(x1), "r"(x2),
                     "r"(x3), "r"(x4), "r"(x5) : "memory", "cc");
    return x0;
}
#else
#if !defined(__x86_64__) || defined(__ILP32__)
#error "Guard supports only AArch64 and x86_64 (not x32)"
#endif
#define G_ABI 2
#define G_AUDIT 0xc000003eU
#define G_NR_read 0
#define G_NR_write 1
#define G_NR_close 3
#define G_NR_openat 257
#define G_NR_dup3 292
#define G_NR_fcntl 72
#define G_NR_ppoll 271
#define G_NR_clock_gettime 228
#define G_NR_getpid 39
#define G_NR_getresuid 118
#define G_NR_getresgid 120
#define G_NR_capget 125
#define G_NR_prctl 157
#define G_NR_rt_sigaction 13
#define G_NR_rt_sigprocmask 14
#define G_NR_getgroups 115
#define G_NR_getrandom 318
#define G_NR_clone 56
#define G_NR_wait4 61
#define G_NR_execve 59
#define G_NR_exit_group 231
#define G_NR_seccomp 317
#define G_NR_ptrace 101
#define G_NR_vm_read 310
#define G_NR_vm_write 311
#define G_NR_setaffinity 203
#define G_NR_setattr 314
#define G_NR_setparam 142
#define G_NR_setscheduler 144
#define G_NR_setpriority 141
#define G_NR_prlimit 302
#define G_NR_setns 308
#define G_NR_unshare 272
#define G_NR_kcmp 312
#define G_NR_perf 298
#define G_NR_bpf 321
#define G_NR_userfaultfd 323
static inline long g_syscall(long nr, long a, long b, long c, long d, long e, long f) {
    register long r10 __asm__("r10") = d, r8 __asm__("r8") = e, r9 __asm__("r9") = f;
    long result;
    __asm__ volatile("syscall" : "=a"(result) : "a"(nr), "D"(a), "S"(b), "d"(c),
                     "r"(r10), "r"(r8), "r"(r9) : "rcx", "r11", "memory", "cc");
    return result;
}
#endif

#define G_CALL(name,a,b,c,d,e,f) g_syscall(G_NR_##name,(long)(a),(long)(b),(long)(c),\
                                         (long)(d),(long)(e),(long)(f))
#define G_NR_close_range 436
struct g_timespec { g_i64 seconds, nanos; };
struct g_pollfd { int fd; g_u16 events, revents; };
struct g_filter { g_u16 code; g_u8 yes, no; g_u32 value; };
struct g_program { g_u16 length; const struct g_filter *instructions; };
struct g_action { void (*handler)(int); unsigned long flags; void (*restorer)(void); g_u64 mask; };
_Static_assert(sizeof(struct g_filter) == 8, "cBPF ABI");
_Static_assert(sizeof(struct g_program) == 16, "sock_fprog ABI");
_Static_assert(sizeof(struct g_action) == 32, "rt_sigaction ABI");
_Static_assert(sizeof(struct g_timespec) == 16, "native timespec ABI");
_Static_assert(__builtin_offsetof(struct g_program, instructions) == 8, "filter pointer ABI");
_Static_assert(__builtin_offsetof(struct g_action, flags) == 8, "signal flags ABI");
_Static_assert(__builtin_offsetof(struct g_action, restorer) == 16, "signal restorer ABI");
_Static_assert(__builtin_offsetof(struct g_action, mask) == 24, "signal mask ABI");

#define G_STMT(code,value) {code,0,0,value}
#define G_JUMP(code,value,yes,no) {code,yes,no,value}
#define G_DENY(number) G_JUMP(0x15,number,0,1),G_STMT(0x06,0x50001)
/* Additional inherited policy, NOT a replacement for the verified Docker policy.
 * Denials cover scheduler/group selectors, memory/FD controls, namespace changes
 * and asynchronous interfaces. Not certified complete until kernel probes/review.
 */
__attribute__((used,section(".jeff.guard.policy")))
static const struct g_filter g_child_policy[] = {
    G_STMT(0x20,4), G_JUMP(0x15,G_AUDIT,1,0), G_STMT(0x06,0x80000000),
    G_STMT(0x20,0), G_JUMP(0x45,0x40000000,0,1), G_STMT(0x06,0x80000000),
    G_DENY(G_NR_setaffinity), G_DENY(G_NR_setattr), G_DENY(G_NR_setparam),
    G_DENY(G_NR_setscheduler), G_DENY(G_NR_setpriority), G_DENY(G_NR_ptrace),
    G_DENY(G_NR_vm_read), G_DENY(G_NR_vm_write), G_DENY(G_NR_setns), G_DENY(G_NR_unshare),
    G_DENY(G_NR_kcmp), G_DENY(G_NR_perf), G_DENY(G_NR_bpf), G_DENY(G_NR_userfaultfd),
    G_DENY(438), G_DENY(434), G_DENY(424), /* pidfd_getfd/open/send_signal */
    G_DENY(440), G_DENY(448), /* process_madvise/mrelease */
    G_DENY(425), G_DENY(426), G_DENY(427), /* io_uring setup/enter/register */
    G_JUMP(0x15,435,0,1), G_STMT(0x06,0x50026), /* clone3: ENOSYS -> libc fallback */
    G_JUMP(0x15,G_NR_prlimit,0,7),
    G_STMT(0x20,16), G_JUMP(0x15,0,1,0), G_STMT(0x06,0x50001),
    G_STMT(0x20,20), G_JUMP(0x15,0,0,1), G_STMT(0x06,0x7fff0000), G_STMT(0x06,0x50001),
    G_JUMP(0x15,G_NR_clone,0,7),
    G_STMT(0x20,20), G_JUMP(0x15,0,1,0), G_STMT(0x06,0x50001),
    G_STMT(0x20,16), G_JUMP(0x45,0x7e820000U,0,1), /* NEW* namespaces, UNTRACED */
    G_STMT(0x06,0x50001), G_STMT(0x06,0x7fff0000),
    G_STMT(0x06,0x7fff0000)
};
#endif
