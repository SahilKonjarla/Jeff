/* Additional native Linux LP64 syscalls. No libc, sysroot or new capabilities.
 * Numbers: cached Linux v6.12 generic_unistd.txt / x86_syscalls.txt references.
 * mmap shared anonymous status is prefaulted before clone; exec removes the
 * child's mapping. Separate address spaces, no CLONE_VM/vfork or status fd leak.
 */
#ifndef JEFF_GUARD_V2_ABI_H
#define JEFF_GUARD_V2_ABI_H
#include "guard_abi.h"
#include "guard_v2_core.h"
#if defined(__aarch64__)
#define G_NR_pipe2 59
#define G_NR_readlinkat 78
#define G_NR_mmap 222
#else
#define G_NR_pipe2 293
#define G_NR_readlinkat 267
#define G_NR_mmap 9
#endif
struct g2_failure { g_u32 code; };
_Static_assert(sizeof(struct g2_failure)==4, "native lock-free status word");
_Static_assert(__atomic_always_lock_free(4,0), "no compiler atomic runtime");
#endif
